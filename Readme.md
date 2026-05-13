# FastAPI — Installation & Setup

## Prerequisites

- Python 3.8+
- [uv](https://astral.sh/uv) (recommended) or pip

---

## 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your terminal after installing so the `uv` command is available.

---

## 2. Create your project

```bash
mkdir my-project
cd my-project
uv venv
source .venv/bin/activate
```

---

## 3. Install dependencies

```bash
uv pip install fastapi uvicorn jinja2 aiofiles aiosqlite
```

---

## 4. Create a minimal app

Create a file called `main.py`:

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Hello, world!"}
```

---

## 5. Start the dev server

```bash
uvicorn main:app --reload
```

Then open your browser at:

```
http://localhost:8000
```

Interactive API docs are available automatically at:

```
http://localhost:8000/docs
```

---

## Project structure (recommended)

```
my-project/
├── main.py              # app entry point
├── routers/             # route handlers, one file per feature
├── templates/           # Jinja2 HTML templates
├── static/              # CSS, JS, images
├── images.db            # SQLite database
└── .venv/               # virtual environment (don't commit this)
```

---

## Daily workflow

Each time you open a new terminal session:

```bash
cd my-project
source .venv/bin/activate
uvicorn main:app --reload
```

`--reload` watches for file changes and restarts the server automatically during development. Remove it in production.

---

## Useful links

- [FastAPI docs](https://fastapi.tiangolo.com)
- [uv docs](https://docs.astral.sh/uv)
- [Jinja2 templates](https://jinja.palletsprojects.com)
- [HTMX](https://htmx.org) — add interactivity to templates without writing JavaScript

---

## AI Image Metadata Scraper

`AiImageScaper.py` scans a directory of AI-generated images and extracts generation metadata into a SQLite database.

### Supported sources

- **SwarmUI** — reads `sui_image_params` PNG chunk
- **Forge / A1111** — parses embedded parameters footer
- **ComfyUI** — decodes `prompt` JSON workflow

### Extracted fields

| Field | Description |
|-------|-------------|
| Model | Base model name |
| LoRAs | Names and weights |
| Prompt / Negative | Full prompt text |
| Seed, Steps, Sampler, CFG | Generation parameters |
| Rating | Star rating from EXIF/XMP (0–5) |
| Hash | SHA-256 content hash (deduplication) |
| Raw metadata | Original metadata for debugging |

### Usage

```bash
# First-time scan
python AiImageScaper.py /path/to/images --db images.db

# Dry run (parse only, no DB writes)
python AiImageScaper.py /path/to/images --db images.db --dry-run

# Verbose output
python AiImageScaper.py /path/to/images --db images.db --verbose
```

### Behavior

- **Idempotent** — safe to run multiple times; duplicate images are skipped via content hash
- **Batch commits** — writes to DB in batches of 100 for efficiency
- **Hash-first skip** — on re-runs, only computes the file hash before skipping already-indexed images

### Database schema

```
images          — one row per unique image (hash-based dedup)
models          — unique model names
loras           — unique LoRA names
image_loras     — many-to-many join (image ↔ LoRA with weight)
```
