import os, uuid, threading, time, logging, subprocess, json
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path("/tmp/vidtools")
DOWNLOAD_DIR.mkdir(exist_ok=True)

jobs = {}

# ── SETUP DEPS ────────────────────────────────────────────────────────────────
def setup_deps():
    cmds = [
        (["apt-get","install","-y","ffmpeg"], "ffmpeg", 120),
        (["pip","install","--upgrade","yt-dlp","-q"], "yt-dlp", 60),
        (["pip","install","spleeter","-q"], "spleeter", 120),
    ]
    for cmd, name, timeout in cmds:
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout)
            log.info(f"{name} ready")
        except Exception as e:
            log.warning(f"{name} setup failed: {e}")

threading.Thread(target=setup_deps, daemon=True).start()

# ── CLEANUP ───────────────────────────────────────────────────────────────────
def cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        for f in DOWNLOAD_DIR.rglob("*"):
            if f.is_file() and now - f.stat().st_mtime > 3600:
                try: f.unlink()
                except: pass
threading.Thread(target=cleanup_loop, daemon=True).start()

# ── HELPERS ───────────────────────────────────────────────────────────────────
def detect_platform(url):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "YouTube"
    if "tiktok.com"  in u: return "TikTok"
    if "instagram.com" in u: return "Instagram"
    if "twitter.com" in u or "x.com" in u: return "Twitter/X"
    if "facebook.com" in u or "fb.com" in u: return "Facebook"
    return "Video"

def safe_name(title):
    return "".join(c for c in title if c.isalnum() or c in " -_").strip() or "video"

def job_update(jid, **kw):
    if jid in jobs:
        jobs[jid].update(kw)

# ── AUDIO SEPARATION (Spleeter) ───────────────────────────────────────────────
def isolate_voice(video_path, job_id, mode="voice_only"):
    """
    mode: 'voice_only' = hapus musik+sfx, sisakan vokal
          'silent'     = hapus semua audio (raw video)
          'music_only' = kebalikan, sisakan musik
    """
    try:
        vpath    = Path(video_path)
        work_dir = DOWNLOAD_DIR / f"spl_{job_id}"
        work_dir.mkdir(exist_ok=True)

        if mode == "silent":
            # Hapus semua audio — paling cepat
            out = DOWNLOAD_DIR / f"{job_id}_silent.mp4"
            subprocess.run([
                "ffmpeg","-y","-i",str(vpath),
                "-c:v","copy","-an",str(out)
            ], check=True, capture_output=True, timeout=300)
            return str(out)

        # Extract audio dulu
        audio_raw = work_dir / "audio.wav"
        subprocess.run([
            "ffmpeg","-y","-i",str(vpath),
            "-vn","-acodec","pcm_s16le","-ar","44100","-ac","2",
            str(audio_raw)
        ], check=True, capture_output=True, timeout=300)

        # Spleeter separate
        job_update(job_id, status="separating_audio")
        subprocess.run([
            "python","-m","spleeter","separate",
            "-p","spleeter:2stems","-o",str(work_dir),
            str(audio_raw)
        ], check=True, capture_output=True, timeout=600)

        stem = "vocals" if mode == "voice_only" else "accompaniment"
        stem_wav = work_dir / "audio" / f"{stem}.wav"
        if not stem_wav.exists():
            log.warning(f"[{job_id}] stem not found, returning original")
            return video_path

        suffix = "_VoiceOnly" if mode == "voice_only" else "_MusicOnly"
        out = DOWNLOAD_DIR / f"{job_id}{suffix}.mp4"
        subprocess.run([
            "ffmpeg","-y",
            "-i",str(vpath),"-i",str(stem_wav),
            "-c:v","copy","-c:a","aac","-b:a","192k",
            "-map","0:v:0","-map","1:a:0","-shortest",
            str(out)
        ], check=True, capture_output=True, timeout=300)

        try:
            import shutil; shutil.rmtree(str(work_dir))
        except: pass

        return str(out)
    except Exception as e:
        log.error(f"[{job_id}] isolate_voice error: {e}")
        return video_path

# ── VIDEO ENHANCE ─────────────────────────────────────────────────────────────
def enhance_video(video_path, job_id, preset="balanced"):
    """
    preset: 'sharp' | 'balanced' | 'smooth'
    Uses FFmpeg filters: unsharp, hqdn3d, eq for color grading
    """
    try:
        vpath = Path(video_path)
        out   = DOWNLOAD_DIR / f"{job_id}_enhanced.mp4"

        filters = {
            "sharp":    "hqdn3d=1:1:3:3,unsharp=5:5:1.2:5:5:0.5,eq=contrast=1.08:brightness=0.02:saturation=1.15",
            "balanced": "hqdn3d=2:2:5:5,unsharp=3:3:0.8:3:3:0.3,eq=contrast=1.05:brightness=0.01:saturation=1.1",
            "smooth":   "hqdn3d=4:4:8:8,unsharp=2:2:0.5:2:2:0.1,eq=contrast=1.03:saturation=1.05",
        }
        vf = filters.get(preset, filters["balanced"])

        # Upscale ke 1080p jika resolusi lebih kecil
        vf_full = f"scale='if(lt(iw,1920),iw*2,iw)':'if(lt(ih,1080),ih*2,ih)':flags=lanczos,{vf}"

        subprocess.run([
            "ffmpeg","-y","-i",str(vpath),
            "-vf", vf_full,
            "-c:v","libx264","-crf","18","-preset","fast",
            "-c:a","copy",
            str(out)
        ], check=True, capture_output=True, timeout=600)

        return str(out)
    except Exception as e:
        log.error(f"[{job_id}] enhance_video error: {e}")
        return video_path

# ── WATERMARK REMOVAL ─────────────────────────────────────────────────────────
def remove_watermark(video_path, job_id, regions):
    """
    regions: list of {x, y, w, h} in percent (0-100) of video dimensions
    Uses FFmpeg delogo + blur to cover watermark areas
    """
    try:
        vpath = Path(video_path)
        out   = DOWNLOAD_DIR / f"{job_id}_nowm.mp4"

        if not regions:
            return video_path

        # Get video dimensions
        probe = subprocess.run([
            "ffprobe","-v","error","-select_streams","v:0",
            "-show_entries","stream=width,height",
            "-of","json",str(vpath)
        ], capture_output=True, text=True, timeout=30)
        info   = json.loads(probe.stdout)
        vid_w  = info["streams"][0]["width"]
        vid_h  = info["streams"][0]["height"]

        # Build filter chain — blur each region
        filters = []
        for i, r in enumerate(regions):
            x = int(r["x"] / 100 * vid_w)
            y = int(r["y"] / 100 * vid_h)
            w = max(int(r["w"] / 100 * vid_w), 4)
            h = max(int(r["h"] / 100 * vid_h), 4)
            # Ensure even numbers for codec
            w = w + (w % 2); h = h + (h % 2)
            x = min(x, vid_w - w); y = min(y, vid_h - h)
            filters.append(
                f"[v{i}]crop={w}:{h}:{x}:{y},boxblur=20:5[blurred{i}];"
                f"[v{i}][blurred{i}]overlay={x}:{y}[v{i+1}]"
            )

        # Build complex filter
        n = len(regions)
        complex_parts = [f"[0:v]null[v0]"] + filters
        complex_filter = ";".join(complex_parts)
        # Simplify: use delogo filter chain instead
        delogo = ",".join(
            f"delogo=x={int(r['x']/100*vid_w)}:y={int(r['y']/100*vid_h)}"
            f":w={max(int(r['w']/100*vid_w),4)}:h={max(int(r['h']/100*vid_h),4)}"
            for r in regions
        )

        subprocess.run([
            "ffmpeg","-y","-i",str(vpath),
            "-vf", delogo,
            "-c:v","libx264","-crf","20","-preset","fast",
            "-c:a","copy",
            str(out)
        ], check=True, capture_output=True, timeout=600)

        return str(out)
    except Exception as e:
        log.error(f"[{job_id}] remove_watermark error: {e}")
        return video_path

# ── DOWNLOAD VIDEO ────────────────────────────────────────────────────────────
def download_video(job_id, url, audio_mode, enhance, wm_regions):
    import yt_dlp
    is_tiktok    = "tiktok.com"    in url.lower()
    is_youtube   = "youtube.com"   in url.lower() or "youtu.be" in url.lower()
    is_shorts    = "/shorts/"      in url.lower()
    is_instagram = "instagram.com" in url.lower()

    # Format per platform
    if is_tiktok:
        fmt = "best[ext=mp4]/best"
    elif is_instagram:
        # Instagram: pakai bestaudio+bestvideo agar ada suara
        fmt = "bestvideo+bestaudio/best[ext=mp4]/best"
    elif is_shorts:
        fmt = "best[ext=mp4]/best"
    else:
        # YouTube & lainnya
        fmt = ("bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]"
               "/bestvideo[ext=mp4]+bestaudio"
               "/best[ext=mp4]/best")

    ydl_opts = {
        "format": fmt,
        "outtmpl": str(DOWNLOAD_DIR / f"{job_id}.%(ext)s"),
        "quiet": True, "no_warnings": True,
        "merge_output_format": "mp4",
        "socket_timeout": 60, "retries": 5,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/112.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if is_youtube:
        # Gunakan iOS client — paling stabil bypass bot detection
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["ios", "tv_embedded", "android"],
            }
        }
        # Shorts: format sederhana
        if is_shorts:
            ydl_opts["format"] = "best[ext=mp4]/best"

    if is_tiktok:
        ydl_opts["extractor_args"] = {"tiktok": {"webpage_download": ["1"]}}

    if is_instagram:
        # Instagram butuh merge audio+video → pastikan ffmpeg tersedia
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }]

    try:
        job_update(job_id, status="downloading")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info  = ydl.extract_info(url, download=True)
            title = (info.get("title") or "video")[:60]

        # Cari file
        candidate = None
        for ext in ["mp4","mkv","webm","mov","m4v"]:
            p = DOWNLOAD_DIR / f"{job_id}.{ext}"
            if p.exists():
                candidate = p; break

        if not candidate:
            job_update(job_id, status="error", error="File tidak ditemukan setelah download.")
            return

        current = str(candidate)
        sname   = safe_name(title)
        suffix  = ""

        # Audio processing
        if audio_mode in ("voice_only","silent","music_only"):
            job_update(job_id, status="processing_audio")
            current = isolate_voice(current, job_id, audio_mode)
            suffix += {"voice_only":"_VoiceOnly","silent":"_Silent","music_only":"_MusicOnly"}[audio_mode]

        # Enhance
        if enhance and enhance != "none":
            job_update(job_id, status="enhancing")
            current = enhance_video(current, job_id, enhance)
            suffix += "_HD"

        # Watermark removal
        if wm_regions:
            job_update(job_id, status="removing_watermark")
            current = remove_watermark(current, job_id, wm_regions)
            suffix += "_NoWM"

        size_mb = round(Path(current).stat().st_size / 1024 / 1024, 1)
        job_update(job_id,
            status="done", file=current,
            filename=f"{sname}{suffix}.mp4",
            platform=detect_platform(url),
            title=title + (f" ({suffix.strip('_').replace('_',' ')})" if suffix else ""),
            size_mb=size_mb
        )
        log.info(f"[{job_id}] Done: {title} {size_mb}MB")

    except Exception as e:
        err = str(e)
        log.error(f"[{job_id}] {err}")
        if "sign in" in err.lower() or "bot" in err.lower():
            msg = "YouTube memblokir sementara. Tunggu 2-3 menit lalu coba lagi."
        elif "private" in err.lower():
            msg = "Video private, tidak bisa didownload."
        elif "login" in err.lower():
            msg = "Video ini memerlukan login. Tidak bisa didownload."
        elif "copyright" in err.lower():
            msg = "Video dilindungi copyright."
        elif "10231" in err or "not available" in err.lower():
            msg = "Video tidak tersedia (mungkin sudah dihapus atau private)."
        elif "429" in err or "rate" in err.lower():
            msg = "Platform membatasi request. Tunggu beberapa menit."
        elif "unsupported" in err.lower():
            msg = "URL tidak didukung. Pastikan link dari platform yang benar."
        elif "format" in err.lower():
            msg = "Format tidak tersedia. Coba lagi atau gunakan link lain."
        else:
            msg = f"Gagal download: {err[:180]}"
        job_update(job_id, status="error", error=msg)

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = open("templates/index.html").read() if Path("templates/index.html").exists() else ""

# Inline HTML
HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VidTools — Video Toolkit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Clash+Display:wght@600;700&family=Cabinet+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080B10;--s1:#0F1318;--s2:#161B22;--s3:#1E2530;
  --border:rgba(255,255,255,0.07);
  --text:#F0EDE6;--muted:#7A808F;
  --g1:#4FFFB0;--g2:#00C97A;
  --p1:#818CF8;--p2:#4F46E5;
  --o1:#FB923C;--o2:#EA580C;
  --r1:#F87171;--r2:#DC2626;
  --r:16px;
}
body{background:var(--bg);color:var(--text);font-family:'Cabinet Grotesk',sans-serif;min-height:100vh;}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:radial-gradient(ellipse 800px 600px at 80% -10%,rgba(79,255,176,.05) 0%,transparent 70%),
             radial-gradient(ellipse 600px 400px at -10% 80%,rgba(129,140,248,.04) 0%,transparent 70%);}

/* NAV */
nav{position:sticky;top:0;z-index:100;background:rgba(8,11,16,.9);
    backdrop-filter:blur(20px);border-bottom:1px solid var(--border);
    padding:0 24px;display:flex;align-items:center;gap:0;overflow-x:auto;}
.nav-logo{font-family:'Clash Display',sans-serif;font-size:20px;font-weight:700;
  background:linear-gradient(135deg,var(--g1),var(--p1));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  padding:16px 20px 16px 0;margin-right:8px;white-space:nowrap;flex-shrink:0;}
.nav-tab{padding:18px 16px;font-size:13px;font-weight:500;color:var(--muted);
  cursor:pointer;border-bottom:2px solid transparent;transition:all .18s;
  white-space:nowrap;flex-shrink:0;}
.nav-tab:hover{color:var(--text);}
.nav-tab.active{color:var(--text);border-bottom-color:var(--g1);}

/* PAGES */
.page{display:none;max-width:700px;margin:0 auto;padding:40px 20px 80px;position:relative;z-index:1;}
.page.active{display:block;}

/* PAGE HEADER */
.page-title{font-family:'Clash Display',sans-serif;font-size:32px;font-weight:700;
  letter-spacing:-.5px;margin-bottom:6px;}
.page-sub{font-size:14px;color:var(--muted);margin-bottom:28px;line-height:1.6;}

/* CARD */
.card{background:var(--s1);border:1px solid var(--border);border-radius:var(--r);padding:22px;margin-bottom:16px;}

/* INPUT ROW */
.inp-row{display:flex;gap:10px;flex-wrap:wrap;}
.url-inp{flex:1;min-width:0;background:var(--s2);border:1px solid var(--border);
  border-radius:10px;color:var(--text);font-family:'Cabinet Grotesk',sans-serif;
  font-size:14px;padding:12px 15px;outline:none;transition:border-color .18s;}
.url-inp::placeholder{color:var(--muted);}
.url-inp:focus{border-color:rgba(79,255,176,.35);}
.go-btn{padding:12px 22px;border-radius:10px;border:none;
  background:linear-gradient(135deg,var(--g1),var(--g2));
  color:#080B10;font-family:'Cabinet Grotesk',sans-serif;
  font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap;
  transition:transform .15s,opacity .15s;}
.go-btn:hover{transform:scale(1.03);}
.go-btn:disabled{opacity:.35;cursor:default;transform:none;}
.go-btn.purple{background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;}
.go-btn.orange{background:linear-gradient(135deg,var(--o1),var(--o2));color:#fff;}
.go-btn.red{background:linear-gradient(135deg,var(--r1),var(--r2));color:#fff;}

/* OPTIONS */
.opts-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px;}
.opt-chip{padding:10px 14px;border-radius:10px;border:1.5px solid var(--border);
  background:var(--s2);cursor:pointer;transition:all .15s;font-size:13px;text-align:center;}
.opt-chip:hover{border-color:rgba(255,255,255,.2);}
.opt-chip.sel-g{border-color:var(--g1);background:rgba(79,255,176,.08);color:var(--g1);}
.opt-chip.sel-p{border-color:var(--p1);background:rgba(129,140,248,.08);color:var(--p1);}
.opt-chip.sel-o{border-color:var(--o1);background:rgba(251,146,60,.08);color:var(--o1);}
.opt-chip-label{font-size:10px;color:var(--muted);display:block;margin-top:3px;}

/* TOGGLE */
.toggle-row{display:flex;align-items:center;gap:12px;padding:12px 0;
  border-top:1px solid var(--border);margin-top:12px;}
.toggle-track{width:40px;height:22px;border-radius:11px;background:var(--s3);
  border:1px solid var(--border);position:relative;cursor:pointer;
  transition:background .2s;flex-shrink:0;}
.toggle-track.on{background:var(--g2);}
.toggle-thumb{width:16px;height:16px;border-radius:50%;background:var(--muted);
  position:absolute;top:2px;left:2px;transition:all .2s;}
.toggle-track.on .toggle-thumb{left:20px;background:#fff;}
.toggle-label{font-size:13px;font-weight:500;}
.toggle-hint{font-size:11px;color:var(--muted);}

/* STATUS */
.status-box{display:none;margin-top:14px;}
.status-inner{display:flex;align-items:center;gap:12px;
  background:var(--s2);border-radius:10px;padding:13px 16px;}
.spinner{width:18px;height:18px;border-radius:50%;flex-shrink:0;
  border:2px solid rgba(79,255,176,.2);border-top-color:var(--g1);
  animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg)}}

/* RESULT */
.result-box{display:none;margin-top:14px;}
.result-inner{background:var(--s1);border:1px solid rgba(79,255,176,.2);
  border-radius:var(--r);padding:20px;}
.result-platform{font-size:10px;font-weight:600;letter-spacing:.6px;
  text-transform:uppercase;color:var(--g1);margin-bottom:5px;}
.result-title{font-family:'Clash Display',sans-serif;font-size:16px;margin-bottom:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.result-size{font-size:11px;color:var(--muted);margin-bottom:14px;}
.dl-btn{display:flex;align-items:center;justify-content:center;gap:8px;
  width:100%;padding:12px;border-radius:10px;border:none;
  background:linear-gradient(135deg,var(--g1),var(--g2));
  color:#080B10;font-family:'Cabinet Grotesk',sans-serif;
  font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;
  transition:transform .15s;}
.dl-btn:hover{transform:scale(1.02);}
.dl-btn.purple{background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;}
.dl-btn.orange{background:linear-gradient(135deg,var(--o1),var(--o2));color:#fff;}

/* ERROR */
.error-box{display:none;margin-top:14px;}
.error-inner{background:var(--s1);border:1px solid rgba(248,113,113,.3);
  border-radius:var(--r);padding:14px 18px;}
.error-title{color:#F87171;font-weight:600;font-size:13px;margin-bottom:4px;}

/* WATERMARK CANVAS */
#wm-preview-wrap{position:relative;margin-top:14px;display:none;}
#wm-canvas{border-radius:10px;cursor:crosshair;max-width:100%;display:block;}
.wm-hint{font-size:12px;color:var(--muted);margin-bottom:8px;}
#wm-regions-list{margin-top:10px;display:flex;flex-direction:column;gap:6px;}
.wm-region-tag{display:flex;align-items:center;justify-content:space-between;
  background:var(--s2);border-radius:8px;padding:8px 12px;font-size:12px;}
.wm-del{color:#F87171;cursor:pointer;font-size:14px;padding:0 4px;}

/* SECTION DIVIDER */
.section-div{height:1px;background:var(--border);margin:16px 0;}

/* INFO TAGS */
.info-tag{display:inline-flex;align-items:center;gap:5px;font-size:11px;
  color:var(--muted);padding:4px 10px;border-radius:20px;
  border:1px solid var(--border);background:var(--s2);margin-right:6px;margin-top:6px;}

@media(max-width:480px){
  .opts-grid{grid-template-columns:1fr 1fr;}
  .inp-row{flex-direction:column;}
  .go-btn{width:100%;}
  nav{gap:0;}
  .nav-tab{padding:16px 12px;font-size:12px;}
}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">VidTools ⚡</div>
  <div class="nav-tab active" onclick="switchTab('download')">⬇ Download</div>
  <div class="nav-tab" onclick="switchTab('audio')">🎵 Hapus Musik</div>
  <div class="nav-tab" onclick="switchTab('enhance')">✨ Enhance HD</div>
  <div class="nav-tab" onclick="switchTab('watermark')">🚫 Hapus Watermark</div>
</nav>

<!-- ══ PAGE: DOWNLOAD ══ -->
<div id="page-download" class="page active">
  <div class="page-title">Download Video</div>
  <p class="page-sub">YouTube, TikTok, Instagram, Twitter/X, Facebook + 1000 platform lainnya.</p>
  <div class="card">
    <div class="inp-row">
      <input class="url-inp" id="dl-url" type="url" placeholder="Paste link video...">
      <button class="go-btn" id="dl-btn" onclick="runDownload()">Download MP4</button>
    </div>
    <div style="margin-top:10px">
      <span class="info-tag">▶ YouTube</span>
      <span class="info-tag">♪ TikTok</span>
      <span class="info-tag">◈ Instagram</span>
      <span class="info-tag">✕ Twitter/X</span>
      <span class="info-tag">ƒ Facebook</span>
    </div>
    <div class="status-box" id="dl-status"><div class="status-inner"><div class="spinner"></div><span id="dl-stxt">Memproses...</span></div></div>
  </div>
  <div class="result-box" id="dl-result">
    <div class="result-inner">
      <div class="result-platform" id="dl-plat"></div>
      <div class="result-title"   id="dl-title"></div>
      <div class="result-size"    id="dl-size"></div>
      <a class="dl-btn" id="dl-link" href="#" download>⬇ Download MP4</a>
    </div>
  </div>
  <div class="error-box" id="dl-err"><div class="error-inner"><div class="error-title">Gagal</div><div id="dl-emsg" style="font-size:13px;color:var(--muted)"></div></div></div>
</div>

<!-- ══ PAGE: AUDIO ══ -->
<div id="page-audio" class="page">
  <div class="page-title">Hapus Musik & Sound</div>
  <p class="page-sub">Paste link video → pilih mode audio → download versi bersih tanpa musik atau sound effect.</p>

  <div class="card">
    <div class="inp-row">
      <input class="url-inp" id="au-url" type="url" placeholder="Paste link video...">
    </div>
    <div class="section-div"></div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:10px;font-weight:500;">Pilih Mode Audio</div>
    <div class="opts-grid">
      <div class="opt-chip sel-g" id="au-opt-voice" onclick="selectAudioMode('voice_only')">
        🎤 Vokal Saja
        <span class="opt-chip-label">Hapus musik + sound effect</span>
      </div>
      <div class="opt-chip" id="au-opt-silent" onclick="selectAudioMode('silent')">
        🔇 Silent Mode
        <span class="opt-chip-label">Hapus semua audio (raw video)</span>
      </div>
      <div class="opt-chip" id="au-opt-music" onclick="selectAudioMode('music_only')">
        🎵 Musik Saja
        <span class="opt-chip-label">Hapus suara manusia, sisakan musik</span>
      </div>
      <div class="opt-chip" id="au-opt-none" onclick="selectAudioMode('none')">
        📹 Tidak Ada
        <span class="opt-chip-label">Download original tanpa modifikasi</span>
      </div>
    </div>
    <div style="margin-top:14px">
      <button class="go-btn" id="au-btn" onclick="runAudio()">Proses Audio</button>
    </div>
    <div id="au-note" style="margin-top:12px;font-size:11px;color:var(--muted);line-height:1.6;">
      ⏱ Mode Vokal Saja membutuhkan 2–5 menit karena proses AI separation.
      Silent Mode paling cepat (instan).
    </div>
    <div class="status-box" id="au-status"><div class="status-inner"><div class="spinner"></div><span id="au-stxt">Memproses...</span></div></div>
  </div>
  <div class="result-box" id="au-result">
    <div class="result-inner">
      <div class="result-platform" id="au-plat"></div>
      <div class="result-title"   id="au-title"></div>
      <div class="result-size"    id="au-size"></div>
      <a class="dl-btn purple" id="au-link" href="#" download>⬇ Download</a>
    </div>
  </div>
  <div class="error-box" id="au-err"><div class="error-inner"><div class="error-title">Gagal</div><div id="au-emsg" style="font-size:13px;color:var(--muted)"></div></div></div>
</div>

<!-- ══ PAGE: ENHANCE ══ -->
<div id="page-enhance" class="page">
  <div class="page-title">Enhance Video HD</div>
  <p class="page-sub">Tingkatkan ketajaman, kurangi noise, dan perbaiki warna. Upscale resolusi 2x jika di bawah 1080p.</p>

  <div class="card">
    <div class="inp-row">
      <input class="url-inp" id="en-url" type="url" placeholder="Paste link video...">
    </div>
    <div class="section-div"></div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:10px;font-weight:500;">Pilih Preset Enhancement</div>
    <div class="opts-grid">
      <div class="opt-chip" id="en-opt-sharp" onclick="selectEnhance('sharp')">
        🔪 Tajam
        <span class="opt-chip-label">Max sharpening + noise reduction</span>
      </div>
      <div class="opt-chip sel-o" id="en-opt-balanced" onclick="selectEnhance('balanced')">
        ⚖️ Balanced
        <span class="opt-chip-label">Optimal untuk kebanyakan video</span>
      </div>
      <div class="opt-chip" id="en-opt-smooth" onclick="selectEnhance('smooth')">
        🌊 Halus
        <span class="opt-chip-label">Kurangi grain, warna lembut</span>
      </div>
      <div class="opt-chip" id="en-opt-none" onclick="selectEnhance('none')">
        📹 Original
        <span class="opt-chip-label">Tanpa enhancement, download saja</span>
      </div>
    </div>
    <div style="margin-top:14px">
      <button class="go-btn orange" id="en-btn" onclick="runEnhance()">Enhance Video</button>
    </div>
    <div style="margin-top:12px;font-size:11px;color:var(--muted);line-height:1.6;">
      ⏱ Proses ~3–8 menit tergantung durasi video. File hasil mungkin lebih besar.
    </div>
    <div class="status-box" id="en-status"><div class="status-inner"><div class="spinner"></div><span id="en-stxt">Memproses...</span></div></div>
  </div>
  <div class="result-box" id="en-result">
    <div class="result-inner">
      <div class="result-platform" id="en-plat"></div>
      <div class="result-title"   id="en-title"></div>
      <div class="result-size"    id="en-size"></div>
      <a class="dl-btn orange" id="en-link" href="#" download>⬇ Download HD</a>
    </div>
  </div>
  <div class="error-box" id="en-err"><div class="error-inner"><div class="error-title">Gagal</div><div id="en-emsg" style="font-size:13px;color:var(--muted)"></div></div></div>
</div>

<!-- ══ PAGE: WATERMARK ══ -->
<div id="page-watermark" class="page">
  <div class="page-title">Hapus Watermark</div>
  <p class="page-sub">Paste link → tandai area watermark di preview → download video tanpa watermark.</p>

  <div class="card">
    <div class="inp-row">
      <input class="url-inp" id="wm-url" type="url" placeholder="Paste link video...">
      <button class="go-btn red" id="wm-load-btn" onclick="loadWMPreview()">Load Preview</button>
    </div>
    <div class="status-box" id="wm-load-status"><div class="status-inner"><div class="spinner"></div><span id="wm-load-stxt">Mengambil thumbnail...</span></div></div>
  </div>

  <div id="wm-step2" style="display:none">
    <div class="card">
      <div class="wm-hint">Klik & drag untuk tandai area watermark. Bisa tandai lebih dari 1 area.</div>
      <canvas id="wm-canvas"></canvas>
      <div id="wm-regions-list"></div>
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
        <button class="go-btn" style="flex:1" id="wm-btn" onclick="runWatermark()">Hapus Watermark</button>
        <button class="go-btn" style="background:var(--s3);color:var(--muted);flex:0 0 auto" onclick="clearWMRegions()">Clear</button>
      </div>
      <div style="margin-top:12px;font-size:11px;color:var(--muted);line-height:1.6;">
        ⏱ Proses ~2–5 menit. Hasilnya tergantung kompleksitas watermark.
        Watermark statis di pojok paling efektif.
      </div>
    </div>
    <div class="status-box" id="wm-status"><div class="status-inner"><div class="spinner"></div><span id="wm-stxt">Memproses...</span></div></div>
    <div class="result-box" id="wm-result">
      <div class="result-inner">
        <div class="result-title" id="wm-title"></div>
        <div class="result-size"  id="wm-size"></div>
        <a class="dl-btn red" id="wm-link" href="#" download>⬇ Download</a>
      </div>
    </div>
    <div class="error-box" id="wm-err"><div class="error-inner"><div class="error-title">Gagal</div><div id="wm-emsg" style="font-size:13px;color:var(--muted)"></div></div></div>
  </div>
</div>

<script>
// ── NAV ───────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
}

// ── GENERIC JOB POLLING ───────────────────────────────────────────────────────
const statusLabels = {
  queued:            "Menunggu antrian...",
  downloading:       "Downloading video...",
  processing_audio:  "AI memisahkan vokal dari musik... (2-5 menit)",
  separating_audio:  "AI separasi audio...",
  enhancing:         "Meningkatkan kualitas video...",
  removing_watermark:"Menghapus watermark...",
};

function poll(jid, statusEl, resultFn, errFn, btnEl) {
  let retries = 0;
  const t = setInterval(async () => {
    try {
      const r = await fetch('/status/' + jid);
      const d = await r.json();
      if (d.status === 'done') {
        clearInterval(t);
        document.getElementById(statusEl).style.display = 'none';
        if (btnEl) document.getElementById(btnEl).disabled = false;
        resultFn(d, jid);
      } else if (d.status === 'error') {
        if (d.error === 'Job not found' && retries < 5) { retries++; return; }
        clearInterval(t);
        document.getElementById(statusEl).style.display = 'none';
        if (btnEl) document.getElementById(btnEl).disabled = false;
        errFn(d.error);
      } else {
        const lbl = statusLabels[d.status] || 'Memproses...';
        document.getElementById(statusEl.replace('-status','-stxt')).textContent = lbl;
      }
    } catch(e) { /* ignore */ }
  }, 2500);
}

async function submitJob(url, opts, statusEl, resultFn, errFn, btnEl) {
  if (!url.startsWith('http')) { alert('Paste URL yang valid!'); return; }
  document.getElementById(btnEl).disabled = true;
  document.getElementById(statusEl).style.display = 'block';
  // hide previous result/error
  const page = statusEl.split('-')[0];
  ['result','err'].forEach(s => {
    const el = document.getElementById(page + '-' + s);
    if (el) el.style.display = 'none';
  });

  try {
    const r = await fetch('/download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, ...opts})
    });
    const d = await r.json();
    if (d.job_id) {
      setTimeout(() => poll(d.job_id, statusEl, resultFn, errFn, btnEl), 2000);
    } else {
      document.getElementById(statusEl).style.display = 'none';
      document.getElementById(btnEl).disabled = false;
      errFn(d.error || 'Gagal');
    }
  } catch(e) {
    document.getElementById(statusEl).style.display = 'none';
    document.getElementById(btnEl).disabled = false;
    errFn('Tidak bisa terhubung ke server.');
  }
}

function showResult(plat, title, size, link, filename, platId, titleId, sizeId, linkId) {
  if (platId)  document.getElementById(platId).textContent  = plat  || '';
  document.getElementById(titleId).textContent = title || 'Video';
  document.getElementById(sizeId).textContent  = size ? size + ' MB' : '';
  const a = document.getElementById(linkId);
  a.href = link; a.download = filename || 'video.mp4';
}

// ── DOWNLOAD ─────────────────────────────────────────────────────────────────
function runDownload() {
  const url = document.getElementById('dl-url').value.trim();
  submitJob(url, {audio_mode:'none', enhance:'none', wm_regions:[]},
    'dl-status',
    (d, jid) => {
      showResult(d.platform,d.title,d.size_mb,'/file/'+jid,d.filename,'dl-plat','dl-title','dl-size','dl-link');
      document.getElementById('dl-result').style.display = 'block';
    },
    msg => {
      document.getElementById('dl-emsg').textContent = msg;
      document.getElementById('dl-err').style.display = 'block';
    },
    'dl-btn'
  );
}

// ── AUDIO ─────────────────────────────────────────────────────────────────────
let audioMode = 'voice_only';
function selectAudioMode(m) {
  audioMode = m;
  ['voice','silent','music','none'].forEach(k => {
    const el = document.getElementById('au-opt-' + k);
    if (el) el.className = 'opt-chip';
  });
  document.getElementById('au-opt-' + m.replace('_only','')).className = 'opt-chip sel-g';
  const notes = {
    voice_only: '⏱ Mode Vokal membutuhkan 2–5 menit karena proses AI separation.',
    silent:     '⚡ Silent Mode: instan! Semua audio dihapus, video tetap utuh.',
    music_only: '⏱ Mode Musik membutuhkan 2–5 menit untuk AI separation.',
    none:       '📹 Download original tanpa modifikasi audio.',
  };
  document.getElementById('au-note').textContent = notes[m] || '';
}

function runAudio() {
  const url = document.getElementById('au-url').value.trim();
  submitJob(url, {audio_mode: audioMode, enhance:'none', wm_regions:[]},
    'au-status',
    (d, jid) => {
      showResult(d.platform,d.title,d.size_mb,'/file/'+jid,d.filename,'au-plat','au-title','au-size','au-link');
      document.getElementById('au-result').style.display = 'block';
    },
    msg => {
      document.getElementById('au-emsg').textContent = msg;
      document.getElementById('au-err').style.display = 'block';
    },
    'au-btn'
  );
}

// ── ENHANCE ───────────────────────────────────────────────────────────────────
let enhancePreset = 'balanced';
function selectEnhance(p) {
  enhancePreset = p;
  ['sharp','balanced','smooth','none'].forEach(k => {
    document.getElementById('en-opt-' + k).className = 'opt-chip';
  });
  document.getElementById('en-opt-' + p).className = 'opt-chip sel-o';
}

function runEnhance() {
  const url = document.getElementById('en-url').value.trim();
  submitJob(url, {audio_mode:'none', enhance: enhancePreset, wm_regions:[]},
    'en-status',
    (d, jid) => {
      showResult(d.platform,d.title,d.size_mb,'/file/'+jid,d.filename,'en-plat','en-title','en-size','en-link');
      document.getElementById('en-result').style.display = 'block';
    },
    msg => {
      document.getElementById('en-emsg').textContent = msg;
      document.getElementById('en-err').style.display = 'block';
    },
    'en-btn'
  );
}

// ── WATERMARK ─────────────────────────────────────────────────────────────────
let wmRegions = [];
let wmDrawing = false;
let wmStart   = {x:0, y:0};
let wmCanvas, wmCtx, wmImg;

async function loadWMPreview() {
  const url = document.getElementById('wm-url').value.trim();
  if (!url.startsWith('http')) { alert('Paste URL dulu!'); return; }
  document.getElementById('wm-load-btn').disabled = true;
  document.getElementById('wm-load-status').style.display = 'block';

  try {
    const r = await fetch('/thumbnail?url=' + encodeURIComponent(url));
    const d = await r.json();
    document.getElementById('wm-load-status').style.display = 'none';
    document.getElementById('wm-load-btn').disabled = false;

    if (d.thumb_url) {
      document.getElementById('wm-step2').style.display = 'block';
      wmCanvas = document.getElementById('wm-canvas');
      wmCtx    = wmCanvas.getContext('2d');
      wmImg    = new Image();
      wmImg.crossOrigin = 'anonymous';
      wmImg.onload = () => {
        const maxW = wmCanvas.parentElement.clientWidth - 2;
        const scale = Math.min(1, maxW / wmImg.naturalWidth);
        wmCanvas.width  = wmImg.naturalWidth  * scale;
        wmCanvas.height = wmImg.naturalHeight * scale;
        redrawCanvas();
      };
      wmImg.src = d.thumb_url;
      setupCanvasEvents();
    } else {
      // No thumbnail — show blank canvas with instructions
      document.getElementById('wm-step2').style.display = 'block';
      wmCanvas = document.getElementById('wm-canvas');
      wmCtx    = wmCanvas.getContext('2d');
      wmCanvas.width  = 640;
      wmCanvas.height = 360;
      wmCtx.fillStyle = '#161B22';
      wmCtx.fillRect(0,0,640,360);
      wmCtx.fillStyle = '#7A808F';
      wmCtx.font = '16px Cabinet Grotesk, sans-serif';
      wmCtx.textAlign = 'center';
      wmCtx.fillText('Preview tidak tersedia. Gambar area watermark secara manual.', 320, 180);
      setupCanvasEvents();
    }
  } catch(e) {
    document.getElementById('wm-load-status').style.display = 'none';
    document.getElementById('wm-load-btn').disabled = false;
    alert('Gagal load preview. Coba lagi.');
  }
}

function setupCanvasEvents() {
  wmRegions = [];
  wmCanvas.onmousedown = e => {
    wmDrawing = true;
    const rect = wmCanvas.getBoundingClientRect();
    wmStart = {x: e.clientX - rect.left, y: e.clientY - rect.top};
  };
  wmCanvas.onmousemove = e => {
    if (!wmDrawing) return;
    const rect = wmCanvas.getBoundingClientRect();
    const cur  = {x: e.clientX - rect.left, y: e.clientY - rect.top};
    redrawCanvas();
    // Draw current selection
    wmCtx.strokeStyle = '#4FFFB0';
    wmCtx.lineWidth   = 2;
    wmCtx.setLineDash([5,3]);
    wmCtx.strokeRect(wmStart.x, wmStart.y, cur.x - wmStart.x, cur.y - wmStart.y);
    wmCtx.setLineDash([]);
  };
  wmCanvas.onmouseup = e => {
    if (!wmDrawing) return;
    wmDrawing = false;
    const rect = wmCanvas.getBoundingClientRect();
    const ex   = e.clientX - rect.left;
    const ey   = e.clientY - rect.top;
    const w    = Math.abs(ex - wmStart.x);
    const h    = Math.abs(ey - wmStart.y);
    if (w < 5 || h < 5) return;
    // Convert to percentage
    wmRegions.push({
      x: Math.min(wmStart.x, ex) / wmCanvas.width  * 100,
      y: Math.min(wmStart.y, ey) / wmCanvas.height * 100,
      w: w / wmCanvas.width  * 100,
      h: h / wmCanvas.height * 100,
    });
    redrawCanvas();
    renderRegionList();
  };
  // Touch support
  wmCanvas.ontouchstart = e => { e.preventDefault(); wmCanvas.onmousedown(e.touches[0]); };
  wmCanvas.ontouchmove  = e => { e.preventDefault(); wmCanvas.onmousemove(e.touches[0]); };
  wmCanvas.ontouchend   = e => { e.preventDefault(); wmCanvas.onmouseup(e.changedTouches[0]); };
}

function redrawCanvas() {
  if (!wmCtx) return;
  wmCtx.clearRect(0,0,wmCanvas.width,wmCanvas.height);
  if (wmImg && wmImg.complete) wmCtx.drawImage(wmImg,0,0,wmCanvas.width,wmCanvas.height);
  wmRegions.forEach((r,i) => {
    const x = r.x/100*wmCanvas.width, y = r.y/100*wmCanvas.height;
    const w = r.w/100*wmCanvas.width, h = r.h/100*wmCanvas.height;
    wmCtx.fillStyle = 'rgba(79,255,176,0.25)';
    wmCtx.fillRect(x,y,w,h);
    wmCtx.strokeStyle = '#4FFFB0';
    wmCtx.lineWidth   = 2;
    wmCtx.strokeRect(x,y,w,h);
    wmCtx.fillStyle   = '#4FFFB0';
    wmCtx.font        = 'bold 12px sans-serif';
    wmCtx.fillText('#' + (i+1), x+4, y+14);
  });
}

function renderRegionList() {
  const el = document.getElementById('wm-regions-list');
  el.innerHTML = wmRegions.map((r,i) =>
    `<div class="wm-region-tag">
      Area #${i+1}: x=${r.x.toFixed(1)}% y=${r.y.toFixed(1)}% w=${r.w.toFixed(1)}% h=${r.h.toFixed(1)}%
      <span class="wm-del" onclick="deleteWMRegion(${i})">✕</span>
    </div>`
  ).join('');
}

function deleteWMRegion(i) {
  wmRegions.splice(i,1);
  redrawCanvas();
  renderRegionList();
}

function clearWMRegions() {
  wmRegions = [];
  redrawCanvas();
  renderRegionList();
}

function runWatermark() {
  if (!wmRegions.length) { alert('Tandai dulu area watermark di preview!'); return; }
  const url = document.getElementById('wm-url').value.trim();
  submitJob(url, {audio_mode:'none', enhance:'none', wm_regions: wmRegions},
    'wm-status',
    (d, jid) => {
      document.getElementById('wm-title').textContent = d.title || 'Video';
      document.getElementById('wm-size').textContent  = d.size_mb ? d.size_mb + ' MB' : '';
      const a = document.getElementById('wm-link');
      a.href = '/file/'+jid; a.download = d.filename || 'video.mp4';
      document.getElementById('wm-result').style.display = 'block';
    },
    msg => {
      document.getElementById('wm-emsg').textContent = msg;
      document.getElementById('wm-err').style.display = 'block';
    },
    'wm-btn'
  );
}

// Auto-paste
document.querySelectorAll('.url-inp').forEach(inp => {
  inp.addEventListener('focus', async () => {
    if (inp.value) return;
    try {
      const t = await navigator.clipboard.readText();
      if (t.startsWith('http')) inp.value = t;
    } catch(e){}
  });
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const page = inp.id.split('-')[0];
      const btns = {dl:'dl-btn',au:'au-btn',en:'en-btn',wm:'wm-load-btn'};
      document.getElementById(btns[page])?.click();
    }
  });
});
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/download", methods=["POST"])
def start_download():
    data       = request.json or {}
    url        = (data.get("url") or "").strip()
    audio_mode = data.get("audio_mode", "none")
    enhance    = data.get("enhance",    "none")
    wm_regions = data.get("wm_regions", [])

    if not url.startswith("http"):
        return jsonify({"error": "URL tidak valid"}), 400

    jid = str(uuid.uuid4())[:12]
    jobs[jid] = {"status":"queued","file":None,"filename":None,"error":None}
    threading.Thread(
        target=download_video,
        args=(jid, url, audio_mode, enhance, wm_regions),
        daemon=True
    ).start()
    return jsonify({"job_id": jid})

@app.route("/status/<jid>")
def status(jid):
    j = jobs.get(jid)
    if not j: return jsonify({"status":"error","error":"Job not found"}), 404
    return jsonify(j)

@app.route("/file/<jid>")
def serve_file(jid):
    j = jobs.get(jid)
    if not j or j["status"] != "done": return "Not ready", 404
    p = j.get("file")
    if not p or not Path(p).exists(): return "File not found", 404
    return send_file(p, as_attachment=True,
                     download_name=j.get("filename","video.mp4"),
                     mimetype="video/mp4")

@app.route("/thumbnail")
def thumbnail():
    """Get YouTube thumbnail URL for watermark preview."""
    url = request.args.get("url","").strip()
    thumb_url = None
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True}) as ydl:
            info = ydl.extract_info(url, download=False)
            thumb_url = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url")
    except Exception as e:
        log.warning(f"thumbnail error: {e}")
    return jsonify({"thumb_url": thumb_url})

@app.route("/health")
def health():
    import shutil
    return jsonify({"status":"ok","ffmpeg":bool(shutil.which("ffmpeg"))})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
