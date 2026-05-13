"""FastAPI UI for browsing the AI image statistics database."""

import argparse
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="AI Image Statistics")
templates = Jinja2Templates(directory="templates")

# Global DB path — set from CLI args
DB_PATH = "images.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def summary_stats() -> dict:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
    models = conn.execute("SELECT COUNT(*) AS c FROM models").fetchone()["c"]
    loras = conn.execute("SELECT COUNT(*) AS c FROM loras").fetchone()["c"]
    conn.close()
    return {"total_images": total, "total_models": models, "total_loras": loras}


def model_counts() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT m.name, COUNT(i.id) AS image_count
        FROM models m
        LEFT JOIN images i ON i.model_id = m.id
        GROUP BY m.id
        ORDER BY image_count DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def lora_counts() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT l.name,
               COUNT(il.image_id) AS usage_count,
               ROUND(AVG(il.weight), 2) AS avg_weight
        FROM loras l
        JOIN image_loras il ON il.lora_id = l.id
        GROUP BY l.id
        ORDER BY usage_count DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stats = summary_stats()
    models = model_counts()
    loras = lora_counts()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "stats": stats, "models": models, "loras": loras},
    )


def main():
    parser = argparse.ArgumentParser(description="AI Image Statistics UI")
    parser.add_argument("--db", default="images.db", help="Path to SQLite database")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    args = parser.parse_args()

    global DB_PATH
    DB_PATH = args.db

    if not Path(DB_PATH).exists():
        print(f"Error: Database '{DB_PATH}' not found. Run AiImageScaper.py first.")
        return

    uvicorn.run(app, host="0.0.0.0", port=args.port, reload=True)


if __name__ == "__main__":
    main()
