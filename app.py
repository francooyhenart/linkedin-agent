"""Postline: planificador local de publicaciones de LinkedIn.

Corre en tu compu: FastAPI + SQLite + un modelo local vía Ollama.
Publica con la API oficial de LinkedIn (nada de scraping ni bots de navegador).
"""
import datetime as dt
import os
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Optional
from urllib.parse import urlencode

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE, "data.db"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
LI_ID = os.getenv("LINKEDIN_CLIENT_ID", "").strip()
LI_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "").strip()
LI_REDIRECT = os.getenv("LINKEDIN_REDIRECT_URI", "http://localhost:8000/auth/linkedin/callback")
# Si la compu estuvo apagada y un post quedó atrasado más que esto, no se publica solo:
# queda como "Publicar a mano" para que decidas vos.
MAX_LATE = dt.timedelta(hours=int(os.getenv("MAX_LATE_HOURS", "12")))

FMT = "%Y-%m-%dT%H:%M"
STATUSES = {"draft", "approved", "ready", "published", "failed"}
DEFAULTS = {
    "post_days": "1,3",  # 0=lunes ... 6=domingo -> martes y jueves
    "post_time": "09:00",
    "max_chars": "300",
    "model": os.getenv("OLLAMA_MODEL", "llama3.1:8b"),
    "style_examples": "",
}
# Frases que delatan texto generado. Se evitan en el prompt y se marcan en el editor.
BANNED = [
    "thrilled", "excited to announce", "excited to share", "delve", "game-changer",
    "game changer", "journey", "leverage", "unlock", "landscape", "fast-paced", "humbled",
    "synergy", "elevate", "harness", "seamless", "robust", "tapestry", "testament",
    "pivotal", "foster", "empower", "dive into", "deep dive", "let's connect", "agree?",
    "thoughts?", "here's the thing", "the best part?", "unpopular opinion", "i'm proud to",
    "in today's world", "navigating", "embark", "realm", "cutting-edge",
]
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]")

publish_lock = threading.Lock()
oauth_states: set[str] = set()


# ---------------------------------------------------------------- base de datos
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS ideas(
              id INTEGER PRIMARY KEY, note TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS posts(
              id INTEGER PRIMARY KEY, idea_id INTEGER, text TEXT NOT NULL DEFAULT '',
              scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
              linkedin_url TEXT, published_at TEXT, error TEXT,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS metrics(
              post_id INTEGER PRIMARY KEY, impressions INTEGER DEFAULT 0,
              reactions INTEGER DEFAULT 0, comments INTEGER DEFAULT 0,
              reposts INTEGER DEFAULT 0, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )


def get_settings() -> dict:
    s = dict(DEFAULTS)
    with db() as con:
        s.update({r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")})
    return s


def set_settings(values: dict):
    with db() as con:
        for k, v in values.items():
            con.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, "" if v is None else str(v)),
            )


POST_SELECT = """
SELECT p.*, COALESCE(m.impressions, 0) impressions, COALESCE(m.reactions, 0) reactions,
       COALESCE(m.comments, 0) comments, COALESCE(m.reposts, 0) reposts
FROM posts p LEFT JOIN metrics m ON m.post_id = p.id
"""


def get_post(pid: int) -> dict:
    with db() as con:
        row = con.execute(POST_SELECT + " WHERE p.id = ?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Ese post no existe.")
    return dict(row)


def norm_when(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(value[:16]).strftime(FMT)
    except (ValueError, TypeError):
        raise HTTPException(400, "Fecha inválida. Usá el formato AAAA-MM-DDTHH:MM.")


# ---------------------------------------------------------------- modelo local
def build_system(s: dict) -> str:
    rules = f"""You write short LinkedIn posts in English for a real person, based on their rough notes (the notes may be in Spanish).
Rules:
- Maximum {s['max_chars']} characters. Usually 2 to 4 short lines.
- Sound like a person typing quickly, not a marketer. Plain words, first person.
- Keep one concrete detail from the notes (a tool, a number, a moment).
- Never invent facts, numbers, names or results that are not in the notes.
- No emojis. No hashtags. No em dashes. No bullet lists. No title, no hook formula.
- Do not end with a question to the audience or a call to action.
- Never use these words or phrases: {', '.join(BANNED)}.
- Output only the post text. No quotes, no preamble."""
    if s.get("style_examples", "").strip():
        rules += (
            "\n\nPosts the author wrote before. Match their tone and rhythm, not their topics:\n---\n"
            + s["style_examples"].strip()
        )
    return rules


def clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # modelos con razonamiento
    text = text.strip().strip('"').strip("“”").strip()
    text = re.sub(r"\s*—\s*", ", ", text)
    text = EMOJI_RE.sub("", text)
    text = re.sub(r"(^|\s)#\w+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def check(text: str, max_chars: int) -> list[str]:
    warnings = []
    if len(text) > max_chars:
        warnings.append(f"Se pasa por {len(text) - max_chars} caracteres.")
    hits = [b for b in BANNED if b in text.lower()]
    if hits:
        warnings.append("Suena a IA: " + ", ".join(hits))
    if text.rstrip().endswith("?"):
        warnings.append("Termina con pregunta al público.")
    return warnings


def ollama_chat(model: str, system: str, user: str) -> str:
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": model,
                "stream": False,
                "options": {"temperature": 0.7},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            timeout=300,
        )
    except requests.RequestException:
        raise HTTPException(502, f"No pude hablar con Ollama en {OLLAMA_URL}. ¿Está abierto?")
    if r.status_code == 404:
        raise HTTPException(502, f"Ollama no tiene el modelo '{model}'. Corré: ollama pull {model}")
    if not r.ok:
        raise HTTPException(502, f"Ollama respondió {r.status_code}: {r.text[:200]}")
    return r.json().get("message", {}).get("content", "")


def generate(note: str, text: str = "", instruction: str = "") -> dict:
    s = get_settings()
    max_chars = int(s["max_chars"])
    system = build_system(s)
    user = f"Notes:\n{note.strip() or '(no notes, use the current draft)'}"
    if text.strip():
        user += f"\n\nCurrent draft:\n{text.strip()}\n\nRewrite the draft."
    if instruction.strip():
        user += f"\n\nExtra instruction: {instruction.strip()}"

    out, warnings = "", []
    for _ in range(3):  # reintenta si se pasa de largo o usa frases prohibidas
        out = clean(ollama_chat(s["model"], system, user))
        warnings = check(out, max_chars)
        if not warnings:
            break
        user += f"\n\nYour last version had problems: {' '.join(warnings)} Fix them:\n{out}"
    return {"text": out, "warnings": warnings}


def ollama_status() -> dict:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        r.raise_for_status()
        return {"ok": True, "models": [m["name"] for m in r.json().get("models", [])]}
    except requests.RequestException:
        return {"ok": False, "models": []}


# ---------------------------------------------------------------- LinkedIn
def li_connected(s: dict) -> bool:
    exp = s.get("li_expires_at", "")
    return bool(s.get("li_token")) and bool(exp) and dt.datetime.fromisoformat(exp) > dt.datetime.now()


def li_publish(text: str, s: dict) -> Optional[str]:
    r = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={
            "Authorization": f"Bearer {s['li_token']}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        },
        json={
            "author": f"urn:li:person:{s['li_sub']}",
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"LinkedIn respondió {r.status_code}: {r.text[:300]}")
    urn = r.headers.get("x-restli-id") or (r.json().get("id") if r.content else None)
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn else None


def update_post(pid: int, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as con:
        con.execute(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), pid))


def publish_post(pid: int, manual_reason: Optional[str] = None):
    with publish_lock:
        post = get_post(pid)
        if post["status"] == "published":
            return
        s = get_settings()
        if manual_reason or not li_connected(s):
            update_post(pid, status="ready",
                        error=manual_reason or "LinkedIn no está conectado: copiá el texto y publicalo a mano.")
            return
        if not post["text"].strip():
            update_post(pid, status="failed", error="El post está vacío.")
            return
        try:
            url = li_publish(post["text"], s)
            update_post(pid, status="published", linkedin_url=url, error=None,
                        published_at=dt.datetime.now().strftime(FMT))
        except Exception as e:  # noqa: BLE001
            update_post(pid, status="failed", error=str(e))


def tick():
    """Cada minuto: publica los posts programados cuya hora ya llegó."""
    now = dt.datetime.now()
    with db() as con:
        due = con.execute(
            "SELECT id, scheduled_at FROM posts WHERE status = 'approved' AND scheduled_at <= ? ORDER BY scheduled_at",
            (now.strftime(FMT),),
        ).fetchall()
    for row in due:
        late = now - dt.datetime.strptime(row["scheduled_at"], FMT)
        reason = None
        if late > MAX_LATE:
            reason = "Se pasó la hora mientras la app estaba cerrada. Revisalo y publicalo a mano o reprogramalo."
        publish_post(row["id"], reason)


# ---------------------------------------------------------------- app
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(_app):
    init_db()
    scheduler.add_job(tick, "interval", minutes=1, next_run_time=dt.datetime.now())
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Postline", lifespan=lifespan)


class PostIn(BaseModel):
    text: str = ""
    scheduled_at: str
    status: str = "draft"
    idea_id: Optional[int] = None


class PostPatch(BaseModel):
    text: Optional[str] = None
    scheduled_at: Optional[str] = None
    status: Optional[str] = None
    linkedin_url: Optional[str] = None


class MetricsIn(BaseModel):
    impressions: int = 0
    reactions: int = 0
    comments: int = 0
    reposts: int = 0


class IdeaIn(BaseModel):
    note: str


class GenIn(BaseModel):
    note: str = ""
    text: str = ""
    instruction: str = ""


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/api/status")
def status():
    s = get_settings()
    return {
        "ollama": ollama_status(),
        "linkedin": {
            "configured": bool(LI_ID and LI_SECRET),
            "connected": li_connected(s),
            "name": s.get("li_name", ""),
            "expires_at": s.get("li_expires_at", ""),
        },
    }


@app.get("/api/settings")
def read_settings():
    s = get_settings()
    public = {k: s[k] for k in DEFAULTS}
    public["banned"] = BANNED
    return public


@app.put("/api/settings")
def write_settings(body: dict):
    values = {k: v for k, v in body.items() if k in DEFAULTS}
    if "post_time" in values and not re.fullmatch(r"\d{2}:\d{2}", str(values["post_time"])):
        raise HTTPException(400, "La hora tiene que ser HH:MM.")
    if "max_chars" in values:
        values["max_chars"] = max(80, min(3000, int(values["max_chars"])))
    set_settings(values)
    return read_settings()


@app.get("/api/posts")
def list_posts(start: Optional[str] = None, end: Optional[str] = None, status: Optional[str] = None):
    q, args = POST_SELECT + " WHERE 1=1", []
    if start:
        q += " AND p.scheduled_at >= ?"
        args.append(start)
    if end:
        q += " AND p.scheduled_at < ?"
        args.append(end)
    if status:
        q += " AND p.status = ?"
        args.append(status)
    with db() as con:
        return [dict(r) for r in con.execute(q + " ORDER BY p.scheduled_at", args)]


@app.post("/api/posts")
def create_post(body: PostIn):
    if body.status not in STATUSES:
        raise HTTPException(400, "Estado inválido.")
    with db() as con:
        cur = con.execute(
            "INSERT INTO posts(idea_id, text, scheduled_at, status) VALUES(?, ?, ?, ?)",
            (body.idea_id, body.text.strip(), norm_when(body.scheduled_at), body.status),
        )
        if body.idea_id:
            con.execute("UPDATE ideas SET used = 1 WHERE id = ?", (body.idea_id,))
        pid = cur.lastrowid
    return get_post(pid)


@app.patch("/api/posts/{pid}")
def patch_post(pid: int, body: PostPatch):
    get_post(pid)
    fields = body.model_dump(exclude_none=True)
    if "status" in fields:
        if fields["status"] not in STATUSES:
            raise HTTPException(400, "Estado inválido.")
        if fields["status"] == "published":
            fields.setdefault("published_at", dt.datetime.now().strftime(FMT))
        fields["error"] = None
    if "scheduled_at" in fields:
        fields["scheduled_at"] = norm_when(fields["scheduled_at"])
    if "text" in fields:
        fields["text"] = fields["text"].strip()
    if fields:
        update_post(pid, **fields)
    return get_post(pid)


@app.delete("/api/posts/{pid}")
def delete_post(pid: int):
    with db() as con:
        con.execute("DELETE FROM metrics WHERE post_id = ?", (pid,))
        con.execute("DELETE FROM posts WHERE id = ?", (pid,))
    return {"ok": True}


@app.post("/api/posts/{pid}/publish-now")
def publish_now(pid: int):
    if not li_connected(get_settings()):
        raise HTTPException(400, "Conectá LinkedIn en Ajustes para publicar desde acá.")
    publish_post(pid)
    post = get_post(pid)
    if post["status"] != "published":
        raise HTTPException(502, post["error"] or "No se pudo publicar.")
    return post


@app.put("/api/posts/{pid}/metrics")
def put_metrics(pid: int, body: MetricsIn):
    get_post(pid)
    with db() as con:
        con.execute(
            "INSERT INTO metrics(post_id, impressions, reactions, comments, reposts, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(post_id) DO UPDATE SET impressions = excluded.impressions, "
            "reactions = excluded.reactions, comments = excluded.comments, reposts = excluded.reposts, "
            "updated_at = excluded.updated_at",
            (pid, body.impressions, body.reactions, body.comments, body.reposts,
             dt.datetime.now().strftime(FMT)),
        )
    return get_post(pid)


@app.get("/api/ideas")
def list_ideas():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM ideas ORDER BY used, id DESC")]


@app.post("/api/ideas")
def add_idea(body: IdeaIn):
    if not body.note.strip():
        raise HTTPException(400, "La idea está vacía.")
    with db() as con:
        cur = con.execute("INSERT INTO ideas(note) VALUES(?)", (body.note.strip(),))
        return dict(con.execute("SELECT * FROM ideas WHERE id = ?", (cur.lastrowid,)).fetchone())


@app.delete("/api/ideas/{iid}")
def delete_idea(iid: int):
    with db() as con:
        con.execute("DELETE FROM ideas WHERE id = ?", (iid,))
    return {"ok": True}


@app.post("/api/generate")
def api_generate(body: GenIn):
    if not body.note.strip() and not body.text.strip():
        raise HTTPException(400, "Escribí una nota con la idea primero.")
    return generate(body.note, body.text, body.instruction)


def next_slots(count: int, s: dict) -> list[str]:
    days = {int(d) for d in s["post_days"].split(",") if d.strip().isdigit()}
    if not days:
        raise HTTPException(400, "Elegí al menos un día de publicación en Ajustes.")
    hh, mm = (int(x) for x in s["post_time"].split(":"))
    with db() as con:
        taken = {r[0][:10] for r in con.execute("SELECT scheduled_at FROM posts")}
    now, slots = dt.datetime.now(), []
    day = now.date()
    while len(slots) < count and (day - now.date()).days < 60:
        when = dt.datetime.combine(day, dt.time(hh, mm))
        if day.weekday() in days and when > now and day.isoformat() not in taken:
            slots.append(when.strftime(FMT))
        day += dt.timedelta(days=1)
    return slots


@app.post("/api/plan")
def plan():
    """Toma las próximas ideas sin usar y arma borradores en los próximos días libres."""
    s = get_settings()
    per_week = len([d for d in s["post_days"].split(",") if d.strip().isdigit()])
    with db() as con:
        ideas = [dict(r) for r in con.execute(
            "SELECT * FROM ideas WHERE used = 0 ORDER BY id LIMIT ?", (per_week,))]
    if not ideas:
        raise HTTPException(400, "No hay ideas sin usar. Cargá algunas en Ideas.")
    slots = next_slots(len(ideas), s)
    created = []
    for idea, when in zip(ideas, slots):
        draft = generate(idea["note"])
        created.append(create_post(PostIn(text=draft["text"], scheduled_at=when,
                                          status="draft", idea_id=idea["id"])))
    return created


@app.get("/auth/linkedin")
def li_login():
    if not (LI_ID and LI_SECRET):
        raise HTTPException(400, "Falta LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET en el archivo .env")
    state = secrets.token_urlsafe(16)
    oauth_states.add(state)
    params = {
        "response_type": "code", "client_id": LI_ID, "redirect_uri": LI_REDIRECT,
        "state": state, "scope": "openid profile w_member_social",
    }
    return RedirectResponse("https://www.linkedin.com/oauth/v2/authorization?" + urlencode(params))


@app.get("/auth/linkedin/callback")
def li_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        raise HTTPException(400, f"LinkedIn rechazó la conexión: {error_description or error}")
    if state not in oauth_states:
        raise HTTPException(400, "Estado OAuth inválido. Volvé a intentar desde Ajustes.")
    oauth_states.discard(state)
    tok = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": LI_REDIRECT,
              "client_id": LI_ID, "client_secret": LI_SECRET},
        timeout=30,
    )
    if not tok.ok:
        raise HTTPException(502, f"No pude obtener el token: {tok.text[:300]}")
    data = tok.json()
    me = requests.get("https://api.linkedin.com/v2/userinfo",
                      headers={"Authorization": f"Bearer {data['access_token']}"}, timeout=30)
    if not me.ok:
        raise HTTPException(502, f"No pude leer tu perfil: {me.text[:300]}")
    info = me.json()
    expires = dt.datetime.now() + dt.timedelta(seconds=int(data.get("expires_in", 0)))
    set_settings({
        "li_token": data["access_token"], "li_sub": info["sub"],
        "li_name": info.get("name", ""), "li_expires_at": expires.isoformat(timespec="minutes"),
    })
    return RedirectResponse("/#ajustes")


@app.post("/api/linkedin/disconnect")
def li_disconnect():
    set_settings({"li_token": "", "li_sub": "", "li_expires_at": ""})
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
