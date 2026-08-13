"""Preflight — веб-интерфейс.

Запуск:  python run_web.py
Открыть: http://127.0.0.1:8000

Файлы читаются прямо из папок сделок, ничего никуда не загружается и не пишется.
"""

from __future__ import annotations

import io
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from preflight import discover, pricing, store                 # noqa: E402
from preflight.cdr import read_cdr                              # noqa: E402
from preflight.checks import Severity, analyze                  # noqa: E402
from preflight.deal import Role, load_deal, parse_folder_name                      # noqa: E402

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Preflight")
app.add_middleware(SessionMiddleware, secret_key="preflight-local-session", max_age=60 * 60 * 24 * 30)
tpl = Jinja2Templates(directory=str(BASE / "templates"))

SEV_NAME = {3: "стоп", 2: "внимание", 1: "к сведению"}
ROLE_ORDER = [Role.FINAL, Role.WORK, Role.PRINT, Role.CUT, Role.RIBBON,
              Role.BACKUP, Role.PREVIEW, Role.OTHER]

tpl.env.globals.update(SEV_NAME=SEV_NAME, quote=quote,
                       POSITIONS=store.POSITIONS, position_name=store.position_name)


# ── вспомогательное ─────────────────────────────────────────────────────────

def current(request: Request):
    uid = request.session.get("uid")
    return store.get_user(uid) if uid else None


def require(request: Request):
    u = current(request)
    if not u:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return u


def scan_root() -> list[dict]:
    """Папки со сделками под корнем — на любой глубине и при любой раскладке."""
    return discover.scan(store.root_dir())


def analysis_to_dict(a) -> dict:
    return {
        "title": a.deal.title,
        "deadline": a.deal.deadline,
        "files": [{"name": f.name, "role": f.role.value, "size": f.size,
                   "mtime": f.mtime.isoformat(timespec="seconds"), "hints": f.hints}
                  for f in a.deal.files],
        "specs": {k: [asdict(s) | {"qty": s.qty} for s in v] for k, v in a.specs.items()},
        "findings": [{"code": f.code, "severity": int(f.severity), "title": f.title,
                      "detail": f.detail, "files": f.files,
                      "confidence": f.confidence} for f in a.findings],
    }


# ── вход ────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if not store.any_users():
        return RedirectResponse("/setup", status_code=303)
    return tpl.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...)):
    u = store.check_user(login, password)
    if not u:
        return tpl.TemplateResponse(request, "login.html",
                                    {"error": "Неверный логин или пароль"}, status_code=401)
    request.session["uid"] = u["id"]
    return RedirectResponse("/", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    if store.any_users():
        return RedirectResponse("/login", status_code=303)
    return tpl.TemplateResponse(request, "setup.html", {})


@app.post("/setup")
def setup(request: Request, login: str = Form(...), name: str = Form(...),
          password: str = Form(...), role: str = Form("tech")):
    if store.any_users():
        return RedirectResponse("/login", status_code=303)
    uid = store.add_user(login, name, password, role)
    request.session["uid"] = uid
    return RedirectResponse("/onboarding", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── список сделок ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request, month: str | None = None, q: str | None = None):
    u = require(request)
    folders = scan_root()
    seen: dict[str, tuple[int, int]] = {}
    for f in folders:
        seen.setdefault(f["period"], (f["year"], f["month"]))
    months = [p for p, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]

    if q:
        ql = q.lower()
        folders = [f for f in folders
                   if ql in f["name"].lower() or ql in f["where"].lower()]
    elif month:
        folders = [f for f in folders if f["period"] == month]
    elif months:
        month = months[0]
        folders = [f for f in folders if f["period"] == month]

    for f in folders:
        run = store.last_run_for(f["path"])
        f["run"] = dict(run) if run else None
        deal = store.get_deal(f["path"])
        f["deal"] = dict(deal) if deal else None

    return tpl.TemplateResponse(request, "index.html", {
        "user": u, "folders": folders, "months": months, "month": month, "q": q or "",
        "root": str(store.root_dir()),
    })


# ── карточка сделки ─────────────────────────────────────────────────────────

@app.get("/deal", response_class=HTMLResponse)
def deal_page(request: Request, path: str, run_id: int | None = None):
    u = require(request)
    folder = Path(unquote(path))
    if not folder.is_dir():
        raise HTTPException(404, "Папка не найдена")

    d = load_deal(folder)
    store.upsert_deal(str(folder), title=d.title, deadline=d.deadline)
    record = store.get_deal(str(folder))

    run = store.get_run(run_id) if run_id else store.last_run_for(str(folder))
    findings = store.run_findings(run["id"]) if run else []

    files = sorted(d.files, key=lambda f: (ROLE_ORDER.index(f.role), f.name))
    previews = [f for f in d.files
                if f.role == Role.FINAL or (f.role in (Role.PRINT, Role.CUT)
                                            and f.path.suffix.lower() == ".cdr")]

    return tpl.TemplateResponse(request, "deal.html", {
        "user": u, "deal": d, "folder": str(folder), "files": files,
        "previews": previews, "run": run, "findings": findings,
        "record": record, "note": store.get_note(d.title),
        "users": store.users(),
    })


@app.post("/deal/run")
def deal_run(request: Request, path: str = Form(...)):
    u = require(request)
    folder = Path(unquote(path))
    if not folder.is_dir():
        raise HTTPException(404, "Папка не найдена")
    d = load_deal(folder)
    a = analyze(d)
    payload = analysis_to_dict(a)
    deal_no = next((f.hints.get("deal_no") for f in d.files if f.hints.get("deal_no")), None)
    run_id = store.save_run(folder=str(folder), title=d.title, deadline=d.deadline,
                            deal_no=deal_no, user_id=u["id"],
                            findings=payload["findings"], payload=payload)
    return RedirectResponse(f"/deal?path={quote(str(folder))}&run_id={run_id}",
                            status_code=303)


@app.post("/finding/verdict")
def finding_verdict(request: Request, finding_id: int = Form(...),
                    verdict: str = Form(...), back: str = Form("/")):
    u = require(request)
    if verdict not in {"ok", "false", "missed"}:
        raise HTTPException(400, "Неизвестная оценка")
    store.set_verdict(finding_id, verdict, u["id"])
    return RedirectResponse(back, status_code=303)


@app.post("/deal/save")
def deal_save(request: Request, path: str = Form(...), status: str = Form("новая"),
              assignee: str = Form(""), reviewer: str = Form(""),
              difficulty: str = Form(""), crm_url: str = Form(""),
              comment: str = Form(""), note: str = Form("")):
    u = require(request)
    folder = unquote(path)
    store.upsert_deal(folder, status=status, assignee=assignee, reviewer=reviewer,
                      difficulty=difficulty, crm_url=crm_url, comment=comment)
    d = load_deal(Path(folder))
    store.set_note(d.title, note, u["id"])
    return RedirectResponse(f"/deal?path={quote(folder)}", status_code=303)


# ── превью файлов ───────────────────────────────────────────────────────────

@app.get("/preview")
def preview(request: Request, path: str, hi: int = 0):
    require(request)
    p = Path(unquote(path))
    root = store.root_dir().resolve()
    try:
        p.resolve().relative_to(root)       # только внутри корня
    except ValueError:
        raise HTTPException(403, "Файл вне рабочего каталога")
    if not p.is_file():
        raise HTTPException(404)

    ext = p.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp"}:
        media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, f"image/{ext[1:]}")
        return Response(p.read_bytes(), media_type=media)

    # У .cdr встроен лишь эскиз 256 px. Если рядом лежит выгруженный из него PDF —
    # рисуем из PDF: там вектор, и разрешение любое.
    if ext == ".cdr":
        twin = next((c for c in (p.with_suffix(".pdf"), p.with_suffix(".PDF"))
                     if c.is_file()), None)
        if twin is not None:
            return _render_vector(twin, hi)
        try:
            png = read_cdr(p).preview_png
        except Exception:
            png = None
        if png:
            return Response(png, media_type="image/png")
        raise HTTPException(404, "В файле нет превью")

    if ext in {".pdf", ".ai", ".eps"}:
        return _render_vector(p, hi)
    raise HTTPException(415, "Тип файла без превью")


def _render_vector(p: Path, hi: int = 0) -> Response:
    """Рендер первой страницы. hi=1 — крупно, для разглядывания."""
    try:
        import pymupdf
        with pymupdf.open(p) as doc:
            pg = doc[0]
            target = 2400 if hi else 640
            z = min(target / max(pg.rect.width, 1), 8 if hi else 3)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
            return Response(pix.tobytes("png"), media_type="image/png")
    except Exception:
        raise HTTPException(404, "Не удалось отрисовать")


# ── история и калибровка ────────────────────────────────────────────────────

@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    u = require(request)
    return tpl.TemplateResponse(request, "runs.html",
                                {"user": u, "runs": store.recent_runs()})


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    u = require(request)
    return tpl.TemplateResponse(request, "rules.html",
                                {"user": u, "stats": store.rule_stats()})


@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request, saved: int = 0, error: int = 0):
    u = require(request)
    return tpl.TemplateResponse(request, "notes.html",
                                {"user": u, "body": store.get_note("global"),
                                 "saved": bool(saved), "error": bool(error)})


@app.post("/notes")
def notes_save(request: Request, body: str = Form(""), confirm: str = Form("")):
    u = require(request)
    if confirm != "yes":
        return RedirectResponse("/notes?error=1", status_code=303)
    store.set_note("global", body, u["id"])
    return RedirectResponse("/notes?saved=1", status_code=303)


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    u = require(request)
    return tpl.TemplateResponse(request, "stats.html", {"user": u, "s": store.stats()})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, error: str | None = None):
    u = require(request)
    return tpl.TemplateResponse(request, "settings.html", {
        "user": u, "root": str(store.root_dir()), "people": store.users(),
        "found": len(scan_root()), "saved": bool(saved), "error": error,
    })


@app.post("/settings")
def settings_save(request: Request, root: str = Form("")):
    require(request)
    root = root.strip().strip('"').strip("'")
    if root:
        p = Path(root).expanduser()
        if not p.is_dir():
            return RedirectResponse(f"/settings?error={quote('Папки нет: ' + str(p))}",
                                    status_code=303)
        store.set_setting("root_dir", str(p))
    else:
        store.set_setting("root_dir", "")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/user")
def settings_add_user(request: Request, name: str = Form(...), login: str = Form(...),
                      password: str = Form(...), role: str = Form("tech")):
    require(request)
    try:
        store.add_user(login, name, password, role)
    except Exception:
        return RedirectResponse(f"/settings?error={quote('Такой логин уже занят')}",
                                status_code=303)
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── О сервисе ───────────────────────────────────────────────────────────────

@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request):
    u = require(request)
    return tpl.TemplateResponse(request, "about.html", {
        "user": u, "models": pricing.MODELS, "profile": pricing.PROFILE,
        "rate": pricing.usd_rub(),
    })


# ── профиль ─────────────────────────────────────────────────────────────────

@app.get("/avatar/{uid}")
def avatar(uid: int):
    u = store.get_user(uid)
    if not u or not u["avatar"]:
        raise HTTPException(404)
    return Response(u["avatar"], media_type=u["avatar_mime"] or "image/png",
                    headers={"Cache-Control": "no-cache"})


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, saved: int = 0, error: str | None = None):
    u = require(request)
    return tpl.TemplateResponse(request, "profile.html",
                                {"user": u, "saved": bool(saved), "error": error})


@app.post("/profile")
async def profile_save(request: Request):
    u = require(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    login = (form.get("login") or "").strip()
    role = form.get("role") or u["role"]
    pwd = (form.get("password") or "").strip()
    drop = form.get("drop_avatar") == "1"

    blob = mime = None
    up = form.get("avatar")
    if up is not None and getattr(up, "filename", ""):
        data = await up.read()
        if len(data) > 3_000_000:
            return RedirectResponse(f"/profile?error={quote('Файл больше 3 МБ')}", status_code=303)
        if not (up.content_type or "").startswith("image/"):
            return RedirectResponse(f"/profile?error={quote('Нужна картинка')}", status_code=303)
        blob, mime = data, up.content_type

    try:
        store.update_profile(u["id"], name=name or None, login=login or None, role=role,
                             avatar=blob, avatar_mime=mime, clear_avatar=drop)
        if pwd:
            store.set_password(u["id"], pwd)
    except Exception:
        return RedirectResponse(f"/profile?error={quote('Такой логин уже занят')}", status_code=303)
    return RedirectResponse("/profile?saved=1", status_code=303)


# ── знакомство при первом входе ─────────────────────────────────────────────

@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, step: int = 1, error: str | None = None):
    u = require(request)
    return tpl.TemplateResponse(request, "onboarding.html", {
        "user": u, "step": step, "error": error,
        "needs_folder": u["role"] in store.POSITIONS_WITH_FILES,
        "root": str(store.root_dir()),
    })


@app.post("/onboarding/folder")
def onboarding_folder(request: Request, root: str = Form("")):
    require(request)
    root = root.strip().strip('"').strip("'")
    if root:
        p = Path(root).expanduser()
        if not p.is_dir():
            return RedirectResponse(
                f"/onboarding?step=1&error={quote('Папки нет: ' + str(p))}", status_code=303)
        store.set_setting("root_dir", str(p))
    return RedirectResponse("/onboarding?step=2", status_code=303)


@app.post("/onboarding/done")
def onboarding_done(request: Request):
    u = require(request)
    store.mark_onboarded(u["id"])
    return RedirectResponse("/", status_code=303)
