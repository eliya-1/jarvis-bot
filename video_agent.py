"""
Video Agent - סוכן עריכת וידאו מבוסס חוקים (בלי AI).

הסוכן מבין פקודות בעברית ובאנגלית לפי מילות מפתח ותבניות קבועות,
ומבצע אותן בעזרת FFmpeg שמותקן על המחשב.

שימוש:
    python video_agent.py                                  # מצב אינטראקטיבי
    python video_agent.py clip.mp4 "חתוך 0:05-0:30, מהירות 1.5, אנכי"
    python video_agent.py clip.mp4 "מתכון טיקטוק" -o out.mp4
    python video_agent.py --batch C:/Videos "הסר שקטים, נרמל קול"
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
RECIPES_FILE = HERE / "video_recipes.json"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv"}
MEDIA_EXT = "mp3|wav|m4a|aac|ogg|flac|mp4|mov|mkv|avi|webm|m4v|wmv|png|jpg|jpeg"

TIME = r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?"
NUM = r"\d+(?:\.\d+)?"

ENCODE_V = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
ENCODE_A = ["-c:a", "aac", "-b:a", "192k"]


class EditError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def parse_time(s: str) -> float:
    total = 0.0
    for part in s.strip().split(":"):
        total = total * 60 + float(part)
    return total


def fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def find_paths(text: str):
    """מוצא נתיבי קבצים בפקודה - בתוך מרכאות או לפי סיומת."""
    pattern = rf'"([^"]+)"|\'([^\']+)\'|(\S+\.(?:{MEDIA_EXT}))\b'
    out = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        p = next(g for g in m.groups() if g)
        if Path(p).suffix.lower().lstrip(".") in MEDIA_EXT.split("|"):
            out.append(p)
    return out


def quoted_text(text: str):
    m = re.search(r'"([^"]+)"|“([^”]+)”|\'([^\']+)\'', text)
    return next(g for g in m.groups() if g) if m else None


def has_any(text: str, words) -> bool:
    return any(w in text for w in words)


def ffpath(p) -> str:
    """בריחה של נתיב לשימוש בתוך פילטר של FFmpeg (חשוב בווינדוס: C:)."""
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def find_font():
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    return next((c for c in candidates if os.path.exists(c)), None)


HEB = re.compile(r"[\u0590-\u05FF]")


LTR_RUN = re.compile(r"%?[A-Za-z0-9](?:[A-Za-z0-9@#:.,/%+_\-' ]*[A-Za-z0-9])?")
MIRROR = str.maketrans("()[]{}<>", ")(][}{><")


def visual_rtl(text: str) -> str:
    """FFmpeg מצייר טקסט משמאל לימין - הופכים עברית כדי שתיראה נכון,
    אבל משאירים מספרים ואנגלית בכיוון הנכון (10:30 נשאר 10:30)."""
    if not HEB.search(text):
        return text
    rev = text[::-1].translate(MIRROR)
    return LTR_RUN.sub(lambda m: m.group(0)[::-1], rev)


def wrap_text(text, fs, w, max_lines=3):
    """שובר טקסט לשורות כך שייכנס ברוחב המסך. מחזיר (שורות, גודל פונט)."""
    while True:
        max_chars = max(4, int(0.9 * w / (0.56 * fs)))
        lines, cur = [], ""
        for word in text.split():
            if cur and len(cur) + 1 + len(word) > max_chars:
                lines.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            lines.append(cur)
        if (len(lines) <= max_lines and all(len(l) <= max_chars for l in lines)) or fs < 20:
            return lines, fs
        fs *= 0.85


def text_vf(agent, text, w, h, size, pos, fade_at=None, enable=None, boxalpha=0.5):
    """מחזיר פילטר drawtext (שורה או כמה שורות ממורכזות) לטקסט בכל שפה.
    size = חלק מגובה המסך (למשל 14 => h/14), pos = top/center/bottom."""
    font = find_font()
    lines, fs = wrap_text(text, min(h / size, w / size * 1.25), w)
    fs = int(fs)
    line_h = int(fs * 1.45)
    block = line_h * len(lines)
    y0 = {"top": int(h * 0.08), "center": (h - block) // 2,
          "bottom": int(h - h * 0.1 - block)}[pos]
    filters = []
    for i, line in enumerate(lines):
        agent.counter += 1
        tf = Path(agent.workdir) / f"text_{agent.counter}.txt"
        tf.write_text(visual_rtl(line), encoding="utf-8")
        parts = [f"textfile={ffpath(tf)}", "expansion=none", f"fontsize={fs}", "fontcolor=white",
                 "box=1", f"boxcolor=black@{boxalpha}", f"boxborderw={max(8, fs // 4)}",
                 "x=(w-text_w)/2", f"y={y0 + i * line_h}"]
        if font:
            parts.insert(0, f"fontfile={ffpath(font)}")
        if fade_at is not None:
            parts.append(f"alpha='if(lt(t,{fade_at}),0,min(1,(t-{fade_at})/0.4))'")
        if enable:
            parts.append(f"enable='{enable}'")
        filters.append("drawtext=" + ":".join(parts))
    return ",".join(filters)


def atempo_chain(speed: float) -> str:
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def check_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("❌ FFmpeg לא מותקן או לא נמצא ב-PATH.")
        print("   בווינדוס: winget install Gyan.FFmpeg   (ואז לפתוח מחדש את הטרמינל)")
        sys.exit(1)


def run_ff(args):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + [str(a) for a in args]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        err = res.stderr.strip().splitlines()
        raise EditError("FFmpeg נכשל: " + (err[-1] if err else "שגיאה לא ידועה"))
    return res


def probe(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise EditError(f"לא הצלחתי לקרוא את הקובץ: {path}")
    data = json.loads(res.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    info = {
        "duration": float(data["format"].get("duration", 0) or 0),
        "size_mb": int(data["format"].get("size", 0)) / 1024 / 1024,
        "has_audio": a is not None,
        "has_video": v is not None,
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": 30.0,
    }
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        if float(d):
            info["fps"] = round(float(n) / float(d), 2)
    return info


# ---------------------------------------------------------------- operations
# כל פעולה: מילות מפתח, פונקציית פענוח (מחזירה פרמטרים או None) ופונקציית ביצוע.

OPS = []


def op(name, keywords, export=False):
    def deco(fn):
        OPS.append({"name": name, "keywords": keywords, "parse": fn, "export": export})
        return fn
    return deco


def simple_pass(agent, inp, out, vf=None, af=None, pre=None):
    info = agent.info(inp)
    args = list(pre or []) + ["-i", inp]
    if vf:
        args += ["-vf", vf] + ENCODE_V
    else:
        args += ["-c:v", "copy"]
    if info["has_audio"]:
        args += (["-af", af] + ENCODE_A) if af else ["-c:a", "copy"]
    args += ["-movflags", "+faststart", out]
    run_ff(args)


# --- זיהוי שקטים (Jump cut אוטומטי) - חייב להיבדק לפני "חתוך"
@op("remove_silence", ["שקט", "silence", "jump cut", "ג'אמפ"])
def p_silence(seg):
    db = re.search(r"(-?\d+)\s*db", seg)
    dur = re.search(rf"({NUM})\s*(?:שניות|שנ|sec|s\b)", seg)
    return {"db": -abs(int(db.group(1))) if db else -30,
            "min": float(dur.group(1)) if dur else 0.6}


def x_silence(agent, inp, out, p):
    info = agent.info(inp)
    if not info["has_audio"]:
        raise EditError("אין שמע בסרטון - אי אפשר לזהות שקטים")
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(inp), "-af",
         f"silencedetect=noise={p['db']}dB:d={p['min']}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", res.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", res.stderr)]
    if not starts:
        return "לא נמצאו שקטים"
    pad, cur, keep = 0.15, 0.0, []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else info["duration"]
        if s + pad > cur:
            keep.append((cur, min(s + pad, info["duration"])))
        cur = max(cur, e - pad)
    if cur < info["duration"]:
        keep.append((cur, info["duration"]))
    keep = [(a, b) for a, b in keep if b - a > 0.05]
    if not keep:
        raise EditError("כל הסרטון שקט")
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
    run_ff(["-i", inp,
            "-vf", f"select='{expr}',setpts=N/FRAME_RATE/TB",
            "-af", f"aselect='{expr}',asetpts=N/SR/TB"] + ENCODE_V + ENCODE_A + [out])
    removed = info["duration"] - sum(b - a for a, b in keep)
    return f"הוסרו {len(starts)} שקטים ({removed:.1f} שניות)"


p_silence.run = x_silence


# --- חיתוך
@op("trim", ["חתוך", "גזור", "קצר", "trim", "cut", "clip"])
def p_trim(seg):
    m = re.search(rf"({TIME})\s*(?:-|–|עד|to|until)\s*({TIME})", seg)
    if m:
        return {"start": parse_time(m.group(1)), "end": parse_time(m.group(2))}
    m = re.search(rf"({NUM})\s*(?:השניות|שניות)?\s*(?:ה)?ראשונות|first\s*({NUM})", seg)
    if m:
        return {"start": 0, "end": float(m.group(1) or m.group(2))}
    m = re.search(rf"({NUM})\s*(?:השניות|שניות)?\s*(?:ה)?אחרונות|last\s*({NUM})", seg)
    if m:
        return {"last": float(m.group(1) or m.group(2))}
    m = re.search(rf"(?:עד|to|until)\s*({TIME})", seg)
    if m:
        return {"start": 0, "end": parse_time(m.group(1))}
    m = re.search(rf"(?:מ-?|מה-?|from)\s*({TIME})", seg)
    if m:
        return {"start": parse_time(m.group(1)), "end": None}
    return None


def x_trim(agent, inp, out, p):
    dur = agent.info(inp)["duration"]
    if "last" in p:
        start, end = max(0, dur - p["last"]), dur
    else:
        start, end = p["start"], p["end"] if p["end"] is not None else dur
    end = min(end, dur)
    if end <= start:
        raise EditError(f"טווח חיתוך לא תקין ({fmt_time(start)} - {fmt_time(end)}), אורך הסרטון {fmt_time(dur)}")
    run_ff(["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", inp]
           + ENCODE_V + ENCODE_A + ["-movflags", "+faststart", out])
    return f"נחתך {fmt_time(start)} → {fmt_time(end)}"


p_trim.run = x_trim


@op("mute", ["השתק", "בלי קול", "ללא קול", "בלי שמע", "הסר קול", "הסר שמע", "mute", "no audio"])
def p_mute(seg):
    return {}


def x_mute(agent, inp, out, p):
    run_ff(["-i", inp, "-c:v", "copy", "-an", out])
    return "הקול הוסר"


p_mute.run = x_mute


@op("music", ["מוזיקה", "מוסיקה", "שיר", "music", "song"])
def p_music(seg):
    paths = find_paths(seg)
    if not paths:
        return None
    vol = re.search(rf"({NUM})\s*%", seg)
    return {"file": paths[0],
            "volume": float(vol.group(1)) / 100 if vol else 0.3,
            "replace": has_any(seg, ["בלבד", "רק", "במקום", "החלף", "replace", "only"])}


def x_music(agent, inp, out, p):
    music = Path(p["file"]).expanduser()
    if not music.exists():
        raise EditError(f"קובץ המוזיקה לא נמצא: {music}")
    info = agent.info(inp)
    base = ["-i", inp, "-stream_loop", "-1", "-i", music]
    if info["has_audio"] and not p["replace"]:
        fc = (f"[1:a]volume={p['volume']}[m];"
              f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
        run_ff(base + ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy"] + ENCODE_A + [out])
        return f"נוספה מוזיקת רקע ({int(p['volume'] * 100)}%)"
    run_ff(base + ["-map", "0:v", "-map", "1:a", "-c:v", "copy"] + ENCODE_A + ["-shortest", out])
    return "הקול הוחלף במוזיקה"


p_music.run = x_music


@op("extract_audio", ["חלץ שמע", "חלץ קול", "חלץ אודיו", "mp3", "extract audio"], export=True)
def p_extract_audio(seg):
    return {}


def x_extract_audio(agent, inp, out, p):
    if not agent.info(inp)["has_audio"]:
        raise EditError("אין שמע בסרטון")
    run_ff(["-i", inp, "-vn", "-c:a", "libmp3lame", "-q:a", "2", out])
    return "השמע יוצא ל-MP3"


p_extract_audio.run = x_extract_audio
p_extract_audio.ext = ".mp3"


@op("normalize", ["נרמל", "איזון קול", "normalize", "loudnorm"])
def p_norm(seg):
    return {}


def x_norm(agent, inp, out, p):
    if not agent.info(inp)["has_audio"]:
        raise EditError("אין שמע בסרטון")
    simple_pass(agent, inp, out, af="loudnorm=I=-16:TP=-1.5:LRA=11")
    return "עוצמת הקול נורמלה (-16 LUFS, מתאים לרשתות)"


p_norm.run = x_norm


@op("denoise", ["רעש", "רעשים", "noise", "denoise"])
def p_denoise(seg):
    return {}


def x_denoise(agent, inp, out, p):
    if not agent.info(inp)["has_audio"]:
        raise EditError("אין שמע בסרטון")
    simple_pass(agent, inp, out, af="highpass=f=80,afftdn=nf=-25")
    return "רעשי רקע הופחתו"


p_denoise.run = x_denoise


@op("volume", ["ווליום", "וליום", "עוצמ", "הגבר", "הנמך", "volume", "louder", "quieter"])
def p_volume(seg):
    m = re.search(rf"({NUM})\s*%", seg)
    if m:
        return {"v": float(m.group(1)) / 100}
    m = re.search(rf"(?:x|פי)\s*({NUM})|({NUM})", seg)
    if m:
        return {"v": float(m.group(1) or m.group(2))}
    if has_any(seg, ["הנמך", "quieter"]):
        return {"v": 0.5}
    return {"v": 1.5}


def x_volume(agent, inp, out, p):
    if not agent.info(inp)["has_audio"]:
        raise EditError("אין שמע בסרטון")
    simple_pass(agent, inp, out, af=f"volume={p['v']}")
    return f"עוצמה: {int(p['v'] * 100)}%"


p_volume.run = x_volume


@op("speed", ["מהירות", "האץ", "האט", "הילוך איטי", "speed", "slow", "fast", "x"])
def p_speed(seg):
    m = re.search(rf"(?<![a-z])x\s*({NUM})|(?<![א-ת])פי\s*({NUM})|({NUM})\s*x\b|(?:מהירות|speed)\s*({NUM})", seg)
    if m:
        s = float(next(g for g in m.groups() if g))
    elif has_any(seg, ["האט", "איטי", "slow"]):
        s = 0.5
    elif has_any(seg, ["האץ", "fast"]):
        s = 2.0
    else:
        return None
    return {"s": s} if 0.1 <= s <= 16 else None


def x_speed(agent, inp, out, p):
    s = p["s"]
    simple_pass(agent, inp, out, vf=f"setpts=PTS/{s}",
                af=atempo_chain(s))
    return f"מהירות x{s:g}"


p_speed.run = x_speed


@op("reverse", ["לאחור", "אחורה", "רוורס", "reverse", "rewind"])
def p_reverse(seg):
    return {}


def x_reverse(agent, inp, out, p):
    if agent.info(inp)["duration"] > 120:
        raise EditError("היפוך לאחור צורך הרבה זיכרון - חתוך קודם לקטע של עד 2 דקות")
    simple_pass(agent, inp, out, vf="reverse", af="areverse")
    return "הסרטון הופך לאחור"


p_reverse.run = x_reverse


@op("grayscale", ["שחור לבן", "שחור-לבן", "שחור ולבן", "grayscale", "black and white", "b&w", "bw"])
def p_gray(seg):
    return {}


def x_gray(agent, inp, out, p):
    simple_pass(agent, inp, out, vf="hue=s=0")
    return "שחור-לבן"


p_gray.run = x_gray


@op("color", ["בהיר", "כהה", "קונטרסט", "ניגודיות", "רוויה", "צבעוני", "brighter", "darker",
              "contrast", "vivid", "saturat"])
def p_color(seg):
    eq = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0}
    if has_any(seg, ["בהיר", "brighter"]):
        eq["brightness"] = 0.08
    if has_any(seg, ["כהה", "darker"]):
        eq["brightness"] = -0.08
    if has_any(seg, ["קונטרסט", "ניגודיות", "contrast"]):
        eq["contrast"] = 1.2
    if has_any(seg, ["רוויה", "צבעוני", "vivid", "saturat"]):
        eq["saturation"] = 1.4
    return eq


def x_color(agent, inp, out, p):
    vf = f"eq=brightness={p['brightness']}:contrast={p['contrast']}:saturation={p['saturation']}"
    simple_pass(agent, inp, out, vf=vf)
    return "צבעים עודכנו"


p_color.run = x_color


@op("sharpen", ["חדד", "חידוד", "sharpen"])
def p_sharpen(seg):
    return {}


def x_sharpen(agent, inp, out, p):
    simple_pass(agent, inp, out, vf="unsharp=5:5:0.8:5:5:0.0")
    return "התמונה חודדה"


p_sharpen.run = x_sharpen


@op("stabilize", ["ייצב", "יצב", "ייצוב", "רעידות", "stabilize", "deshake"])
def p_stab(seg):
    return {}


def x_stab(agent, inp, out, p):
    simple_pass(agent, inp, out, vf="deshake")
    return "הסרטון יוצב"


p_stab.run = x_stab


@op("rotate", ["סובב", "סיבוב", "rotate"])
def p_rotate(seg):
    m = re.search(r"(90|180|270)", seg)
    deg = int(m.group(1)) if m else 90
    if has_any(seg, ["שמאל", "left"]) and deg == 90:
        deg = 270
    return {"deg": deg}


def x_rotate(agent, inp, out, p):
    vf = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}[p["deg"]]
    simple_pass(agent, inp, out, vf=vf)
    return f"סובב {p['deg']}°"


p_rotate.run = x_rotate


@op("mirror", ["מראה", "mirror", "flip"])
def p_mirror(seg):
    return {"v": has_any(seg, ["אנכי", "vertical", "למטה"])}


def x_mirror(agent, inp, out, p):
    simple_pass(agent, inp, out, vf="vflip" if p["v"] else "hflip")
    return "הופך כמו מראה"


p_mirror.run = x_mirror


@op("aspect", ["אנכי", "טיקטוק", "tiktok", "רילס", "reels", "שורטס", "shorts", "9:16",
               "ריבוע", "מרובע", "square", "1:1", "אופקי", "16:9", "יוטיוב", "youtube"])
def p_aspect(seg):
    if has_any(seg, ["ריבוע", "מרובע", "square", "1:1"]):
        w, h = 1080, 1080
    elif has_any(seg, ["אופקי", "16:9", "יוטיוב", "youtube"]):
        w, h = 1920, 1080
    else:
        w, h = 1080, 1920
    return {"w": w, "h": h, "blur": has_any(seg, ["טשטוש", "מטושטש", "blur", "רקע"])}


def x_aspect(agent, inp, out, p):
    w, h = p["w"], p["h"]
    info = agent.info(inp)
    if p["blur"]:
        fc = (f"[0:v]split[a][b];"
              f"[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bg];"
              f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
        args = ["-i", inp, "-filter_complex", fc, "-map", "[v]"]
        if info["has_audio"]:
            args += ["-map", "0:a", "-c:a", "copy"]
        run_ff(args + ENCODE_V + ["-movflags", "+faststart", out])
    else:
        vf = (f"crop='min(iw,ih*{w}/{h})':'min(ih,iw*{h}/{w})',scale={w}:{h},setsar=1")
        simple_pass(agent, inp, out, vf=vf)
    return f"פורמט {w}x{h}" + (" עם רקע מטושטש" if p["blur"] else "")


p_aspect.run = x_aspect


@op("resize", ["רזולוציה", "resolution", "resize", "2160p", "1440p", "1080p", "720p", "480p", "360p", "4k"])
def p_resize(seg):
    if "4k" in seg:
        return {"h": 2160}
    m = re.search(r"(2160|1440|1080|720|480|360)", seg)
    return {"h": int(m.group(1))} if m else None


def x_resize(agent, inp, out, p):
    simple_pass(agent, inp, out, vf=f"scale=-2:{p['h']}")
    return f"רזולוציה {p['h']}p"


p_resize.run = x_resize


@op("fade", ["פייד", "fade", "דהייה", "דהיה", "הדרגתי"])
def p_fade(seg):
    m = re.search(rf"({NUM})", seg)
    d = float(m.group(1)) if m else 1.0
    only_in = has_any(seg, ["כניסה", "התחלה", "fade in", "in "])
    only_out = has_any(seg, ["יציאה", "סוף", "fade out", "out"])
    return {"d": d, "in": not only_out or only_in, "out": not only_in or only_out}


def x_fade(agent, inp, out, p):
    dur, d = agent.info(inp)["duration"], p["d"]
    d = min(d, dur / 2)
    vf, af = [], []
    if p["in"]:
        vf.append(f"fade=t=in:st=0:d={d}")
        af.append(f"afade=t=in:st=0:d={d}")
    if p["out"]:
        vf.append(f"fade=t=out:st={dur - d:.3f}:d={d}")
        af.append(f"afade=t=out:st={dur - d:.3f}:d={d}")
    simple_pass(agent, inp, out, vf=",".join(vf), af=",".join(af))
    return f"פייד {d:g} שניות"


p_fade.run = x_fade


@op("text", ["טקסט", "כתוב", "כותרת", "כיתוב", "text", "title", "caption"])
def p_text(seg):
    txt = quoted_text(seg)
    if not txt:
        m = re.search(r"(?:טקסט|כתוב|כותרת|כיתוב|text|title|caption)\s+(.+)", seg)
        if not m:
            return None
        txt = m.group(1)
    pos = "bottom"
    rest = seg.replace(txt, "")
    if has_any(rest, ["למעלה", "עליון", "top"]):
        pos = "top"
    elif has_any(rest, ["באמצע", "במרכז", "מרכז", "center", "middle"]):
        pos = "center"
    rng = re.search(rf"({TIME})\s*(?:-|–|עד|to)\s*({TIME})", rest)
    size = "big" if has_any(rest, ["גדול", "big", "large"]) else (
        "small" if has_any(rest, ["קטן", "small"]) else "normal")
    return {"text": txt.strip(), "pos": pos, "size": size,
            "range": (parse_time(rng.group(1)), parse_time(rng.group(2))) if rng else None}


def x_text(agent, inp, out, p):
    info = agent.info(inp)
    size = {"big": 9, "normal": 14, "small": 22}[p["size"]]
    enable = f"between(t,{p['range'][0]},{p['range'][1]})" if p["range"] else None
    vf = text_vf(agent, p["text"], info["width"], info["height"], size, p["pos"], enable=enable)
    simple_pass(agent, inp, out, vf=vf)
    return f"נוסף טקסט: {p['text']}"


p_text.run = x_text


@op("logo", ["לוגו", "סימן מים", "סמל", "logo", "watermark"])
def p_logo(seg):
    paths = find_paths(seg)
    if not paths:
        return None
    pos = "tr"
    if has_any(seg, ["שמאל למעלה", "top left"]):
        pos = "tl"
    elif has_any(seg, ["שמאל למטה", "bottom left"]):
        pos = "bl"
    elif has_any(seg, ["ימין למטה", "bottom right", "למטה"]):
        pos = "br"
    return {"file": paths[0], "pos": pos}


def x_logo(agent, inp, out, p):
    logo = Path(p["file"]).expanduser()
    if not logo.exists():
        raise EditError(f"קובץ הלוגו לא נמצא: {logo}")
    xy = {"tr": "W-w-30:30", "tl": "30:30", "br": "W-w-30:H-h-30", "bl": "30:H-h-30"}[p["pos"]]
    lw = max(16, int(agent.info(inp)["width"] * 0.15) // 2 * 2)
    fc = f"[1:v]scale={lw}:-1[l];[0:v][l]overlay={xy}[v]"
    args = ["-i", inp, "-i", logo, "-filter_complex", fc, "-map", "[v]"]
    if agent.info(inp)["has_audio"]:
        args += ["-map", "0:a", "-c:a", "copy"]
    run_ff(args + ENCODE_V + [out])
    return "נוסף לוגו"


p_logo.run = x_logo


@op("concat", ["חבר", "מזג", "הוסף בסוף", "הוסף סרטון", "merge", "concat", "join", "append"])
def p_concat(seg):
    paths = find_paths(seg)
    if not paths:
        return None
    return {"files": paths, "before": has_any(seg, ["בהתחלה", "לפני", "before", "intro", "פתיח"])}


def x_concat(agent, inp, out, p):
    files = [Path(f).expanduser() for f in p["files"]]
    for f in files:
        if not f.exists():
            raise EditError(f"הקובץ לא נמצא: {f}")
    seq = (files + [Path(inp)]) if p["before"] else ([Path(inp)] + files)
    base = agent.info(inp)
    w, h = base["width"] // 2 * 2, base["height"] // 2 * 2
    fps = base["fps"] or 30
    args, fc, links = [], [], []
    for i, f in enumerate(seq):
        args += ["-i", f]
        inf = agent.info(f)
        fc.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
        if inf["has_audio"]:
            fc.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        else:
            fc.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={inf['duration']:.3f}[a{i}]")
        links.append(f"[v{i}][a{i}]")
    fc.append("".join(links) + f"concat=n={len(seq)}:v=1:a=1[v][a]")
    run_ff(args + ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]"]
           + ENCODE_V + ENCODE_A + ["-movflags", "+faststart", out])
    return f"חוברו {len(seq)} סרטונים"


p_concat.run = x_concat


@op("compress", ["דחוס", "דחיסה", "הקטן קובץ", "הקטן משקל", "compress", "smaller"])
def p_compress(seg):
    m = re.search(rf"({NUM})\s*(?:mb|מגה|מ\"ב)", seg)
    return {"mb": float(m.group(1)) if m else None}


def x_compress(agent, inp, out, p):
    info = agent.info(inp)
    args = ["-i", inp, "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
    if p["mb"]:
        kbps = int(p["mb"] * 8192 / max(info["duration"], 1) * 0.95) - 128
        if kbps < 100:
            raise EditError("הגודל המבוקש קטן מדי לאורך הסרטון")
        args += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k"]
    else:
        args += ["-crf", "28"]
    if info["has_audio"]:
        args += ["-c:a", "aac", "-b:a", "128k"]
    run_ff(args + ["-movflags", "+faststart", out])
    return f"נדחס ({info['size_mb']:.1f}MB → {os.path.getsize(out) / 1048576:.1f}MB)"


p_compress.run = x_compress


@op("gif", ["gif", "גיף", "ג'יף"], export=True)
def p_gif(seg):
    m = re.search(r"(\d{3,4})\s*(?:px|פיקסל)?", seg)
    return {"w": int(m.group(1)) if m else 480}


def x_gif(agent, inp, out, p):
    fc = (f"fps=12,scale={p['w']}:-1:flags=lanczos,split[a][b];"
          f"[a]palettegen=stats_mode=diff[pl];[b][pl]paletteuse=dither=bayer:bayer_scale=4")
    run_ff(["-i", inp, "-filter_complex", fc, "-loop", "0", out])
    return "נוצר GIF"


p_gif.run = x_gif
p_gif.ext = ".gif"


@op("screenshot", ["צילום מסך", "צלם", "תמונה", "פריים", "תמונה ממוזערת", "thumbnail",
                   "screenshot", "frame", "snapshot"], export=True)
def p_shot(seg):
    m = re.search(rf"({TIME})", seg)
    return {"t": parse_time(m.group(1)) if m else None}


def x_shot(agent, inp, out, p):
    dur = agent.info(inp)["duration"]
    t = p["t"] if p["t"] is not None else dur / 2
    run_ff(["-ss", f"{min(t, max(dur - 0.1, 0)):.3f}", "-i", inp, "-frames:v", "1", "-q:v", "2", out])
    return f"צולם פריים בזמן {fmt_time(t)}"


p_shot.run = x_shot
p_shot.ext = ".png"


# ---------------------------------------------------------------- create from images
# יצירת סרטון מאפס מתמונות + קובץ "רעיון" - הכל לפי חוקים, בלי AI.

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
IDEA_FILES = ["רעיון.txt", "idea.txt", "תסריט.txt", "script.txt"]
FPS = 30

STYLES = {
    "calm":      {"sec": 4.0, "trans": 1.2,  "zoom": 0.12, "fx": ["fade", "dissolve"]},
    "normal":    {"sec": 3.0, "trans": 0.8,  "zoom": 0.15, "fx": ["fade", "smoothleft", "circleopen", "smoothright"]},
    "energetic": {"sec": 2.0, "trans": 0.35, "zoom": 0.22, "fx": ["slideleft", "slideup", "wipeleft", "slideright", "circleopen", "radial"]},
}
STYLE_WORDS = {
    "calm": ["רגוע", "עדין", "רומנטי", "איטי", "נוסטלגי", "calm", "slow", "soft"],
    "energetic": ["אנרגטי", "מהיר", "קצבי", "דינמי", "מקפיץ", "energetic", "fast", "dynamic"],
    "normal": ["רגיל", "normal"],
}
FORMATS = {"vertical": (1080, 1920), "horizontal": (1920, 1080), "square": (1080, 1080)}
CREATE_RE = re.compile(r"^\s*(צור|תצור|יצור|תיצור|תעשה|create|make|מצגת|slideshow)\b", re.IGNORECASE)
SETTING_KEYS = {
    "title": ["כותרת", "title"], "outro": ["סיום", "outro", "end"], "style": ["סגנון", "style"],
    "format": ["פורמט", "format"], "sec": ["שניות", "seconds"], "length": ["אורך", "length"],
    "music": ["מוזיקה", "מוסיקה", "שיר", "music", "song"], "transition": ["מעבר", "transition"],
}


def natural_key(p):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", Path(p).name.lower())]


def detect_format(text):
    if has_any(text, ["ריבוע", "מרובע", "square", "1:1"]):
        return "square"
    if has_any(text, ["אנכי", "טיקטוק", "tiktok", "רילס", "reels", "שורטס", "shorts", "סטורי", "story", "9:16"]):
        return "vertical"
    if has_any(text, ["אופקי", "16:9", "יוטיוב", "youtube", "רחב"]):
        return "horizontal"
    return None


def detect_style(text):
    for name, words in STYLE_WORDS.items():
        if has_any(text, words):
            return name
    return None


def read_idea(folder: Path, opts: dict):
    """קורא את קובץ הרעיון (רעיון.txt) מהתיקייה, אם קיים."""
    f = next((folder / n for n in IDEA_FILES if (folder / n).exists()), None)
    if not f:
        return None
    images = {p.name.lower(): p.name for p in folder.iterdir()}
    stems = {p.stem.lower(): p.name for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXT}
    for raw in f.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or set(line) <= {"-", "="}:
            continue
        key, _, val = line.partition(":")
        k, val = key.strip().lower(), val.strip().strip('"')
        setting = next((s for s, words in SETTING_KEYS.items() if k in words), None) if val else None
        if setting == "style":
            opts["style"] = detect_style(val.lower()) or opts["style"]
        elif setting == "format":
            opts["format"] = detect_format(val.lower()) or opts["format"]
        elif setting in ("sec", "length"):
            m = re.search(TIME, val)
            if m:
                opts[setting] = parse_time(m.group(0))
        elif setting == "music":
            p = Path(val)
            opts["music"] = str(p if p.is_absolute() else folder / p)
        elif setting:
            opts[setting] = val
        elif val and (k in images or k in stems):
            opts["captions"][images.get(k) or stems[k]] = val
        else:
            opts["seq_captions"].append(line)
    return f.name


def parse_create_opts(seg: str, opts: dict) -> bool:
    """מעדכן אפשרויות יצירה מתוך קטע פקודה. מחזיר True אם הקטע היה אפשרות יצירה."""
    low = seg.lower()
    hit = False
    q = quoted_text(seg)
    if re.search(r"(כותרת|title)", low):
        opts["title"] = q or re.sub(r".*?(כותרת|title)\s*[:=]?\s*", "", seg, flags=re.I).strip()
        return True
    if re.search(r"(סיום|outro|\bend\b)", low):
        opts["outro"] = q or re.sub(r".*?(סיום|outro|end)\s*[:=]?\s*", "", seg, flags=re.I).strip()
        return True
    if has_any(low, SETTING_KEYS["music"]):
        paths = find_paths(seg)
        if paths:
            opts["music"] = paths[0]
            return True
    m = re.search(rf"(?:אורך|length|total)\s*({TIME})|({TIME})\s*(?:שניות)?\s*(?:סה\"כ|סהכ|בסך הכל|total)", low)
    if m:
        opts["length"] = parse_time(m.group(1) or m.group(2))
        hit = True
    else:
        m = re.search(rf"({NUM})\s*(?:שניות|שנ'|sec|s)\b|(?:שניות|seconds)\s*({NUM})", low)
        if m:
            opts["sec"] = float(m.group(1) or m.group(2))
            hit = True
    fmt, style = detect_format(low), detect_style(low)
    if fmt:
        opts["format"] = fmt
        hit = True
    if style:
        opts["style"] = style
        hit = True
    if has_any(low, ["בלי זום", "ללא זום", "no zoom"]):
        opts["zoom"] = False
        hit = True
    if has_any(low, ["בלי מעברים", "ללא מעברים", "no transition"]):
        opts["transitions"] = False
        hit = True
    if has_any(low, ["רקע מטושטש", "טשטוש", "בלי חיתוך", "blur", "fit"]):
        opts["fit"] = "blur"
        hit = True
    m = re.search(r"(?:מעבר|transition)\s+([a-z]+)", low)
    if m:
        opts["fx"] = m.group(1)
        hit = True
    return hit


def default_create_opts():
    return {"style": "normal", "format": None, "sec": None, "length": None, "title": None,
            "outro": None, "music": None, "zoom": True, "transitions": True, "fit": "cover",
            "fx": None, "captions": {}, "seq_captions": []}


def make_image_clip(agent, img, out, w, h, dur, i, opts, caption=None, title=None):
    st = STYLES[opts["style"]]
    frames = max(1, round(dur * FPS))
    W2, H2 = w * 2, h * 2                                   # הגדלה כפולה = זום חלק בלי רעידות
    if opts["fit"] == "blur":
        base = (f"split[a][b];[a]scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2},"
                f"boxblur=40:10[bg];[b]scale={W2}:{H2}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        base = f"scale={W2}:{H2}:force_original_aspect_ratio=increase,crop={W2}:{H2}"
    z = st["zoom"] if opts["zoom"] else 0
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    moves = [  # מתחלפים בין תמונות כדי שלא ישעמם
        (f"1+{z}*on/{frames}", cx, cy),
        (f"1+{z}-{z}*on/{frames}", cx, cy),
        (f"1+{z}", f"(iw-iw/zoom)*on/{frames}", cy),
        (f"1+{z}", f"(iw-iw/zoom)*(1-on/{frames})", cy),
    ]
    zx, xx, yy = moves[i % len(moves)] if z else ("1", "0", "0")
    vf = (f"{base},zoompan=z='{zx}':x='{xx}':y='{yy}':d={frames}:s={w}x{h}:fps={FPS},"
          f"setsar=1,format=yuv420p")
    if title:
        vf += "," + text_vf(agent, title, w, h, 11, "center", fade_at=0.2, boxalpha=0.45)
    if caption:
        vf += "," + text_vf(agent, caption, w, h, 18, "bottom", fade_at=0.3, boxalpha=0.45)
    run_ff(["-i", img, "-filter_complex", vf, "-frames:v", frames, "-r", FPS] + ENCODE_V + [out])
    return frames / FPS


def create_video(agent, images, opts, out, log=print):
    if not images:
        raise EditError("לא נמצאו תמונות (jpg / png / webp)")
    if not opts["format"]:
        dims = [agent.info(p) for p in images]
        portrait = sum(d["height"] > d["width"] for d in dims)
        opts["format"] = "vertical" if portrait >= len(images) / 2 else "horizontal"
    w, h = FORMATS[opts["format"]]
    st = STYLES[opts["style"]]
    n = len(images)
    outro_d = 2.5 if opts["outro"] else 0
    T = st["trans"] if opts["transitions"] and n > 1 else 0
    sec = opts["sec"] or st["sec"]
    if opts["length"]:                       # אורך כולל מבוקש -> מחשבים כמה זמן לכל תמונה
        title_d = 0.8 if opts["title"] else 0
        sec = (opts["length"] - outro_d - title_d + (n - 1 + (1 if outro_d else 0)) * T) / n
    sec = max(sec, 1.0)
    T = min(T, sec / 2)
    fx_list = [opts["fx"]] if opts["fx"] else st["fx"]

    seq = list(opts["seq_captions"])
    clips, durs = [], []
    for i, img in enumerate(images):
        cap = opts["captions"].get(Path(img).name) or (seq.pop(0) if seq else None)
        d = sec + (0.8 if i == 0 and opts["title"] else 0)
        clip = os.path.join(agent.workdir, f"clip_{i}.mp4")
        log(f"   🖼️ {i + 1}/{n} {Path(img).name}" + (f"  «{cap}»" if cap else ""))
        durs.append(make_image_clip(agent, img, clip, w, h, d, i, opts, cap,
                                    opts["title"] if i == 0 else None))
        clips.append(clip)
    if outro_d:
        clip = os.path.join(agent.workdir, "clip_outro.mp4")
        run_ff(["-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={FPS}:d={outro_d}",
                "-vf", text_vf(agent, opts["outro"], w, h, 14, "center", fade_at=0.4 + T, boxalpha=0)] + ENCODE_V + [clip])
        clips.append(clip)
        durs.append(outro_d)

    args, fc = [], []
    for c in clips:
        args += ["-i", c]
    if T and len(clips) > 1:
        prev, off = "[0:v]", 0.0
        for k in range(1, len(clips)):
            off += durs[k - 1] - T
            label = f"[x{k}]"
            fc.append(f"{prev}[{k}:v]xfade=transition={fx_list[(k - 1) % len(fx_list)]}:"
                      f"duration={T:.3f}:offset={off:.3f}{label}")
            prev = label
        vout = prev
    else:
        fc.append("".join(f"[{k}:v]" for k in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[cv]")
        vout = "[cv]"
    total = sum(durs) - T * (len(clips) - 1)
    args += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    run_ff(args + ["-filter_complex", ";".join(fc), "-map", vout, "-map", f"{len(clips)}:a"]
           + ENCODE_V + ENCODE_A + ["-shortest", "-movflags", "+faststart", out])
    return f"נוצר סרטון {w}x{h} מ-{n} תמונות ({fmt_time(total)}, סגנון {opts['style']})"


# ---------------------------------------------------------------- the agent

HELP = """
📽️  פקודות שהסוכן מבין (אפשר לשרשר עם פסיק או "ואז"):

 חיתוך      חתוך 0:10-0:45 | חתוך 15 שניות ראשונות | חתוך 10 אחרונות | חתוך מ 1:20
 שקטים      הסר שקטים  (ג'אמפ-קאט אוטומטי)  | הסר שקטים -35db 1 שניות
 מהירות     מהירות 2 | x1.5 | האט | הילוך איטי
 קול        השתק | ווליום 150% | הגבר | הנמך | נרמל קול | נקה רעש
 מוזיקה     מוזיקה song.mp3 20%  | מוזיקה song.mp3 בלבד (מחליף את הקול)
 פורמט      אנכי (טיקטוק/רילס) | אנכי עם רקע מטושטש | ריבוע | אופקי | 720p | 1080p
 מראה       שחור לבן | בהיר | כהה | קונטרסט | צבעוני | חדד | ייצב | מראה
 סיבוב      סובב 90 | סובב שמאלה | סובב 180 | לאחור (רוורס)
 טקסט       טקסט "שלום לכולם" למעלה גדול | טקסט "מבצע!" 0:02-0:06
 לוגו       לוגו logo.png | לוגו logo.png שמאל למטה
 חיבור      חבר part2.mp4 part3.mp4 | חבר intro.mp4 בהתחלה
 מעברים     פייד | פייד 2 | פייד כניסה | פייד יציאה
 דחיסה      דחוס | דחוס ל 25mb
 ייצוא      gif | mp3 | צילום מסך 0:05
 מתכונים    מתכון טיקטוק  (מוגדרים ב-video_recipes.json)

 ✨ יצירה מאפס מתמונות:
            צור "C:\\Photos\\טיול"                       (כל התמונות בתיקייה, לפי סדר השמות)
            צור "C:\\Photos\\טיול", אנכי, אנרגטי, אורך 30, כותרת "הטיול שלנו", סיום "תודה!"
            צור a.jpg b.jpg c.jpg, 4 שניות, רגוע, מוזיקה song.mp3
            אפשרויות: אנכי/ריבוע/אופקי | רגוע/רגיל/אנרגטי | N שניות (לתמונה) | אורך N (סה"כ)
                      כותרת "..." | סיום "..." | מוזיקה x.mp3 | בלי זום | בלי מעברים | רקע מטושטש
            ואפשר להוסיף אחרי זה כל פקודת עריכה רגילה: , טקסט "..." , לוגו logo.png , פייד
            רעיון.txt בתיקייה = התסריט (כותרת / סגנון / מוזיקה / כיתוב לכל תמונה) - ראה VIDEO_AGENT.md

 ניהול      פתח <קובץ> | שמור [קובץ] | בטל | מידע | היסטוריה | עזרה | יציאה
 קבצים עם רווחים בשם - לשים במרכאות: "C:\\My Videos\\clip.mp4"
"""

SPLIT_RE = re.compile(r"\s*(?:,|;|\n|\+|\bואז\b|\bאחר כך\b|\bthen\b|\band then\b)\s*", re.IGNORECASE)


def load_recipes():
    if RECIPES_FILE.exists():
        try:
            return json.loads(RECIPES_FILE.read_text(encoding="utf-8"))
        except Exception:
            print("⚠️ קובץ המתכונים פגום")
    return {}


class VideoAgent:
    def __init__(self, output_dir=None):
        self.workdir = tempfile.mkdtemp(prefix="video_agent_")
        self.output_dir = output_dir
        self.source = None
        self.history = []          # [(path, description)]
        self.saved_at = 1          # אורך ההיסטוריה בשמירה האחרונה
        self.counter = 0
        self._info_cache = {}
        self.recipes = load_recipes()

    # -- state
    @property
    def current(self):
        return self.history[-1][0] if self.history else None

    def info(self, path):
        key = str(path)
        if key not in self._info_cache:
            self._info_cache[key] = probe(path)
        return self._info_cache[key]

    def load(self, path):
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise EditError(f"הקובץ לא נמצא: {p}")
        inf = self.info(p)
        if not inf["has_video"]:
            raise EditError("זה לא קובץ וידאו")
        self.source = p
        self.history = [(str(p), "מקור")]
        self.saved_at = 1
        return f"📂 נטען {p.name} — {self.describe(p)}"

    def describe(self, path):
        i = self.info(path)
        return (f"{i['width']}x{i['height']}, {fmt_time(i['duration'])}, {i['fps']:g}fps, "
                f"{i['size_mb']:.1f}MB, {'עם' if i['has_audio'] else 'בלי'} קול")

    def default_output(self, suffix="_edited", ext=".mp4"):
        folder = Path(self.output_dir) if self.output_dir else self.source.parent
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{self.source.stem}{suffix}{ext}"
        n = 2
        while out.exists():
            out = folder / f"{self.source.stem}{suffix}_{n}{ext}"
            n += 1
        return out

    def save(self, path=None):
        if not self.current:
            raise EditError("אין סרטון פתוח")
        if not self.dirty:
            raise EditError("אין שינויים חדשים לשמור")
        out = Path(path).expanduser() if path else self.default_output()
        if not out.suffix:
            out = out.with_suffix(".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.current, out)
        self.saved_at = len(self.history)
        return f"💾 נשמר: {out}", str(out)

    @property
    def dirty(self):
        return bool(self.history) and self.saved_at != len(self.history)

    def create(self, text, log=print):
        """צור <תיקייה או תמונות>, אפשרויות... , פקודות עריכה... -> סרטון חדש."""
        segments = [s for s in SPLIT_RE.split(text) if s and s.strip()]
        first = CREATE_RE.sub("", segments[0], count=1).strip()
        first = re.sub(r"^(?:סרטון|וידאו|video)?\s*(?:מ|מה|from)?\s*(?:התמונות|תמונות|התיקייה|תיקייה|images|photos)?\s*",
                       "", first, flags=re.I).strip()
        m = re.match(r'\s*"([^"]+)"|\s*\'([^\']+)\'', first)
        if m:
            paths = [m.group(1) or m.group(2)]
            first = first[m.end():]
            paths += find_paths(first)
        else:
            paths = find_paths(first)
            if not paths and first:
                paths = [first.split()[0]]
        for p in paths:
            first = first.replace(p, " ")
        if not paths:
            raise EditError('צריך להגיד מאיפה התמונות, למשל: צור "C:\\Photos\\טיול"')

        images, folder = [], None
        for p in paths:
            pp = Path(p.strip('"\'')).expanduser()
            if pp.is_dir():
                folder = folder or pp
                images += sorted((f for f in pp.iterdir() if f.suffix.lower() in IMAGE_EXT), key=natural_key)
            elif pp.suffix.lower() in IMAGE_EXT and pp.exists():
                folder = folder or pp.parent
                images.append(pp)
            else:
                raise EditError(f"לא נמצא: {pp}")

        opts = default_create_opts()
        idea = read_idea(folder, opts)
        parse_create_opts(first, opts)
        rest, absorbing = [], True
        for seg in segments[1:]:
            if absorbing and parse_create_opts(seg, opts):
                continue
            absorbing = False
            rest.append(seg)

        log(f"✨ יוצר סרטון מ-{len(images)} תמונות" + (f" (לפי {idea})" if idea else "") + " ...")
        self.counter += 1
        out = os.path.join(self.workdir, f"created_{self.counter}.mp4")
        msg = create_video(self, [str(i) for i in images], opts, out, log)
        self.source = (folder / (folder.name or "video")).resolve()
        self.history = [(out, "נוצר מתמונות")]
        self.saved_at = 0
        if opts["music"]:
            music = Path(opts["music"]).expanduser()
            self.counter += 1
            step = os.path.join(self.workdir, f"step_{self.counter}.mp4")
            x_music(self, out, step, {"file": str(music), "volume": 1.0, "replace": True})
            self.counter += 1
            step2 = os.path.join(self.workdir, f"step_{self.counter}.mp4")
            dur = self.info(step)["duration"]
            simple_pass(self, step, step2, af=f"afade=t=out:st={max(0, dur - 2):.3f}:d=2")
            self.history[0] = (step2, "נוצר מתמונות + מוזיקה")
            msg += f" + מוזיקה {music.name}"
        log(f"✅ {msg}  [{self.describe(self.current)}]")
        return ", ".join(rest)

    def undo(self):
        if len(self.history) <= 1:
            return "אין מה לבטל"
        _, desc = self.history.pop()
        return f"↩️ בוטל: {desc}"

    # -- parsing
    def parse(self, text):
        """מפרק פקודה לרשימת פעולות. מחזיר (פעולות, קטעים שלא הובנו)."""
        segments = [s for s in SPLIT_RE.split(text) if s and s.strip()]
        actions, unknown = [], []
        for seg in segments:
            low = seg.lower().strip()
            m = re.match(r"(?:מתכון|recipe)\s+(.+)", low)
            if m:
                name = m.group(1).strip()
                if name not in self.recipes:
                    unknown.append(f"{seg} (מתכונים קיימים: {', '.join(self.recipes) or 'אין'})")
                    continue
                sub, bad = self.parse(self.recipes[name])
                actions += sub
                unknown += bad
                continue
            for o in OPS:
                if has_any(low, o["keywords"]):
                    params = o["parse"](low if not find_paths(seg) and not quoted_text(seg) else seg)
                    if params is not None:
                        actions.append((o, params, seg.strip()))
                        break
            else:
                unknown.append(seg.strip())
        return actions, unknown

    # -- execution
    def execute(self, text, log=print):
        """מריץ שרשרת פעולות. מחזיר רשימת קבצי ייצוא (gif/mp3/png) שנוצרו."""
        if CREATE_RE.match(text):
            text = self.create(text, log)
            if not text:
                return []
        if not self.current:
            raise EditError("אין סרטון פתוח. כתוב: פתח <נתיב לקובץ>  או  צור <תיקיית תמונות>")
        actions, unknown = self.parse(text)
        for u in unknown:
            log(f"🤷 לא הבנתי: {u}")
        exports = []
        for o, params, seg in actions:
            self.counter += 1
            fn = o["parse"]
            if o["export"]:
                out = str(self.default_output(f"_{o['name']}_{self.counter}", fn.ext))
            else:
                out = os.path.join(self.workdir, f"step_{self.counter}.mp4")
            log(f"⏳ {seg} ...")
            try:
                msg = fn.run(self, self.current, out, params)
            except EditError as e:
                log(f"❌ {seg}: {e}")
                log("   (עוצר את השרשרת - הפעולות הקודמות נשמרו, אפשר 'בטל')")
                break
            if o["export"]:
                exports.append(out)
                log(f"✅ {msg} → {out}")
            elif os.path.exists(out):
                self.history.append((out, seg.strip()))
                log(f"✅ {msg}  [{self.describe(out)}]")
            else:
                log(f"ℹ️ {msg}")
        return exports

    def cleanup(self):
        shutil.rmtree(self.workdir, ignore_errors=True)


# ---------------------------------------------------------------- CLI

def repl(agent):
    print("🎬 סוכן עריכת וידאו (ללא AI) — כתוב 'עזרה' לרשימת פקודות, 'יציאה' לסיום.")
    while True:
        try:
            line = input("\n🎬 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        try:
            if low in ("יציאה", "צא", "exit", "quit", "q"):
                if agent.dirty:
                    try:
                        ans = input("יש שינויים שלא נשמרו. לשמור? (כ/ל) ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        ans = ""
                    if ans in ("כ", "כן", "y", "yes"):
                        print(agent.save()[0])
                break
            if low in ("עזרה", "help", "?"):
                print(HELP)
            elif re.match(r"^(פתח|טען|open|load)\s+", low):
                path = find_paths(line) or [line.split(None, 1)[1]]
                print(agent.load(path[0].strip('"\'')))
            elif re.match(r"^(שמור|save)\b", low):
                rest = line.split(None, 1)[1].strip('"\'') if len(line.split(None, 1)) > 1 else None
                print(agent.save(rest)[0])
            elif low in ("בטל", "undo", "u"):
                print(agent.undo())
            elif low in ("מידע", "info"):
                print(agent.describe(agent.current) if agent.current else "אין סרטון פתוח")
            elif low in ("היסטוריה", "history"):
                for i, (_, d) in enumerate(agent.history):
                    print(f"  {i}. {d}")
            elif low in ("מתכונים", "recipes"):
                for k, v in agent.recipes.items():
                    print(f"  {k}: {v}")
            else:
                if not agent.current and not CREATE_RE.match(line):
                    p = find_paths(line)
                    if p and Path(p[0]).suffix.lower() in VIDEO_EXT:
                        print(agent.load(p[0]))
                        continue
                agent.execute(line)
        except EditError as e:
            print(f"❌ {e}")


def main():
    ap = argparse.ArgumentParser(description="סוכן עריכת וידאו מבוסס חוקים (FFmpeg)")
    ap.add_argument("input", nargs="?", help="קובץ וידאו, או תיקיית תמונות ליצירת סרטון מאפס")
    ap.add_argument("command", nargs="?", help='פקודות, למשל: "חתוך 0:05-0:20, אנכי"')
    ap.add_argument("-o", "--output", help="קובץ פלט")
    ap.add_argument("--batch", metavar="FOLDER", help="להריץ את הפקודה על כל הסרטונים בתיקייה")
    args = ap.parse_args()
    check_ffmpeg()

    if args.batch:
        command = args.input if args.command is None else args.command
        if not command:
            ap.error('צריך פקודה: --batch FOLDER "פקודה"')
        folder = Path(args.batch)
        files = sorted(f for f in folder.iterdir() if f.suffix.lower() in VIDEO_EXT)
        outdir = Path(args.output) if args.output else folder / "edited"
        print(f"📁 {len(files)} סרטונים → {outdir}")
        for f in files:
            agent = VideoAgent(output_dir=outdir)
            try:
                print(f"\n=== {f.name} ===")
                agent.load(f)
                agent.execute(command)
                if agent.dirty:
                    print(agent.save(outdir / f"{f.stem}.mp4")[0])
            except EditError as e:
                print(f"❌ {e}")
            finally:
                agent.cleanup()
        return

    agent = VideoAgent()
    try:
        if args.input and Path(args.input).is_dir():
            agent.execute(f'צור "{args.input}", ' + (args.command or ""))
            print(agent.save(args.output)[0])
            return
        if args.input:
            print(agent.load(args.input))
        if args.input and args.command:
            agent.execute(args.command)
            if agent.dirty:
                print(agent.save(args.output)[0])
        else:
            repl(agent)
    except EditError as e:
        print(f"❌ {e}")
        sys.exit(1)
    finally:
        agent.cleanup()


if __name__ == "__main__":
    main()
