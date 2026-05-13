#!/usr/bin/env python3
"""
AI Image Metadata Scraper
Supports: SwarmUI, ForgeUI/A1111, ComfyUI
Extracts: model, LoRAs + weights, prompt, seed, steps, sampler, CFG, star rating
Output: SQLite database
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
    import piexif
except ImportError:
    print("Missing dependencies. Install with:\n  pip install Pillow piexif")
    sys.exit(1)

BATCH_SIZE = 100  # images per DB commit


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS loras (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath    TEXT NOT NULL,
    filename    TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE,
    width       INTEGER,
    height      INTEGER,
    source_ui   TEXT,           -- 'swarmui' | 'forge' | 'comfyui' | 'unknown'
    model_id    INTEGER REFERENCES models(id),
    prompt      TEXT,
    negative    TEXT,
    seed        INTEGER,
    steps       INTEGER,
    sampler     TEXT,
    cfg         REAL,
    rating      INTEGER,        -- 0-5 stars from EXIF/XMP
    raw_meta    TEXT            -- full original metadata string/JSON for debugging
);

CREATE TABLE IF NOT EXISTS image_loras (
    image_id    INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    lora_id     INTEGER NOT NULL REFERENCES loras(id),
    weight      REAL,
    PRIMARY KEY (image_id, lora_id)
);

CREATE INDEX IF NOT EXISTS idx_images_model   ON images(model_id);
CREATE INDEX IF NOT EXISTS idx_images_hash    ON images(hash);
CREATE INDEX IF NOT EXISTS idx_image_loras_im ON image_loras(image_id);
CREATE INDEX IF NOT EXISTS idx_image_loras_lo ON image_loras(lora_id);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_or_create(conn: sqlite3.Connection, table: str, name: str) -> int:
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Rating extraction (EXIF / XMP)
# ---------------------------------------------------------------------------

def extract_rating(img: Image.Image, filepath: Path) -> Optional[int]:
    """
    Try to read a star rating (0-5) from:
    1. EXIF tag 0x4746 (Rating, used by Windows/most tools)
    2. XMP metadata embedded in the PNG info dict
    """
    # EXIF rating tag
    try:
        exif_data = img.info.get("exif")
        if exif_data:
            exif_dict = piexif.load(exif_data)
            rating_tag = 0x4746
            for ifd in exif_dict.values():
                if isinstance(ifd, dict) and rating_tag in ifd:
                    return int(ifd[rating_tag])
    except Exception:
        pass

    # XMP embedded in PNG tEXt / iTXt chunks
    xmp_str = img.info.get("XML:com.adobe.xmp") or img.info.get("xmp") or ""
    if xmp_str:
        m = re.search(r'xmp:Rating["\s>]+(\d)', xmp_str)
        if m:
            return int(m.group(1))

    return None


# ---------------------------------------------------------------------------
# SwarmUI parser
# ---------------------------------------------------------------------------

def parse_swarmui(info: dict) -> Optional[dict]:
    raw = info.get("sui_image_params")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    loras = []
    for entry in data.get("loras", []):
        name = entry.get("name", "").replace(".safetensors", "")
        weight = entry.get("weight", entry.get("strength", 1.0))
        if name:
            loras.append({"name": name, "weight": float(weight)})

    model = data.get("model", data.get("baseModel", ""))
    # SwarmUI often includes a path prefix — take just the filename stem
    model = Path(model).stem if model else ""

    return {
        "source_ui": "swarmui",
        "model": model,
        "loras": loras,
        "prompt": data.get("prompt", ""),
        "negative": data.get("negativeprompt", data.get("negative_prompt", "")),
        "seed": data.get("seed"),
        "steps": data.get("steps"),
        "sampler": data.get("sampler", ""),
        "cfg": data.get("cfgscale", data.get("cfg_scale")),
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Forge / A1111 parser
# ---------------------------------------------------------------------------

FORGE_FOOTER_RE = re.compile(
    r"Steps:\s*(?P<steps>\d+).*?"
    r"(?:Sampler:\s*(?P<sampler>[^,]+),\s*)?"
    r"(?:CFG scale:\s*(?P<cfg>[\d.]+),\s*)?"
    r"(?:Seed:\s*(?P<seed>\d+),?\s*)?"
    r".*?Model:\s*(?P<model>[^,\n]+)?",
    re.IGNORECASE | re.DOTALL,
)

LORA_RE = re.compile(r"<lora:(?P<name>[^:>]+):(?P<weight>[\d]+(?:\.[\d]+)?)>")


def parse_forge(info: dict) -> Optional[dict]:
    raw = info.get("parameters")
    if not raw:
        return None

    # Split prompt / negative / footer
    parts = raw.split("Negative prompt:", 1)
    prompt = parts[0].strip()
    rest = parts[1] if len(parts) > 1 else ""

    neg_parts = rest.split("\nSteps:", 1)
    negative = neg_parts[0].strip()
    footer = ("Steps:" + neg_parts[1]) if len(neg_parts) > 1 else rest

    loras = []
    for m in LORA_RE.finditer(prompt + " " + footer):
        try:
            loras.append({
                "name": m.group("name").replace(".safetensors", ""),
                "weight": float(m.group("weight")),
            })
        except ValueError:
            pass  # skip malformed weight values

    m = FORGE_FOOTER_RE.search(footer)
    model = m.group("model").strip() if m and m.group("model") else ""
    model = Path(model).stem if model else ""

    return {
        "source_ui": "forge",
        "model": model,
        "loras": loras,
        "prompt": LORA_RE.sub("", prompt).strip(),
        "negative": negative,
        "seed": int(m.group("seed")) if m and m.group("seed") else None,
        "steps": int(m.group("steps")) if m and m.group("steps") else None,
        "sampler": m.group("sampler").strip() if m and m.group("sampler") else "",
        "cfg": float(m.group("cfg")) if m and m.group("cfg") else None,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# ComfyUI parser
# ---------------------------------------------------------------------------

def _find_comfy_nodes(workflow: dict, class_types: list) -> list:
    return [
        node for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") in class_types
    ]


def parse_comfyui(info: dict) -> Optional[dict]:
    raw = info.get("prompt")
    if not raw:
        return None
    try:
        workflow = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(workflow, dict):
        return None

    # LoRA nodes
    lora_nodes = _find_comfy_nodes(workflow, [
        "LoraLoader", "LoraLoaderModelOnly",
        "Power Lora Loader (rgthree)", "LoRALoader",
    ])
    loras = []
    for node in lora_nodes:
        inp = node.get("inputs", {})
        name = inp.get("lora_name", "")
        name = Path(name).stem if name else ""
        weight = inp.get("strength_model", inp.get("lora_strength", inp.get("strength", 1.0)))
        if name:
            loras.append({"name": name, "weight": float(weight)})

    # Model
    ckpt_nodes = _find_comfy_nodes(workflow, [
        "CheckpointLoaderSimple", "CheckpointLoader",
        "UNETLoader", "unCLIPCheckpointLoader",
    ])
    model = ""
    if ckpt_nodes:
        ckpt_name = ckpt_nodes[0].get("inputs", {}).get("ckpt_name", "")
        model = Path(ckpt_name).stem if ckpt_name else ""

    # KSampler for generation params
    samplers = _find_comfy_nodes(workflow, [
        "KSampler", "KSamplerAdvanced", "SamplerCustom",
    ])
    seed = steps = sampler = cfg = None
    if samplers:
        inp = samplers[0].get("inputs", {})
        seed = inp.get("seed", inp.get("noise_seed"))
        steps = inp.get("steps")
        sampler = inp.get("sampler_name", "")
        cfg = inp.get("cfg")

    # Prompt text — look for CLIPTextEncode nodes
    prompt_nodes = _find_comfy_nodes(workflow, ["CLIPTextEncode"])
    prompts = [n.get("inputs", {}).get("text", "") for n in prompt_nodes]
    prompt = prompts[0] if prompts else ""
    negative = prompts[1] if len(prompts) > 1 else ""

    return {
        "source_ui": "comfyui",
        "model": model,
        "loras": loras,
        "prompt": prompt,
        "negative": negative,
        "seed": int(seed) if seed is not None else None,
        "steps": int(steps) if steps is not None else None,
        "sampler": sampler,
        "cfg": float(cfg) if cfg is not None else None,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Per-image processing
# ---------------------------------------------------------------------------

def compute_hash(filepath: Path) -> str:
    """SHA-256 hash of the raw file bytes."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_image(filepath: Path) -> Optional[dict]:
    """Open image, parse metadata. Does NOT compute hash — caller passes that in."""
    img = None
    try:
        img = Image.open(filepath)
        info = img.info or {}
    except Exception as e:
        print(f"  [WARN] Cannot open {filepath}: {e}")
        return None

    meta = (
        parse_swarmui(info)
        or parse_forge(info)
        or parse_comfyui(info)
    )

    if not meta:
        meta = {
            "source_ui": "unknown",
            "model": "",
            "loras": [],
            "prompt": "",
            "negative": "",
            "seed": None,
            "steps": None,
            "sampler": "",
            "cfg": None,
            "raw": str(list(info.keys())),
        }

    meta["rating"] = extract_rating(img, filepath)
    meta["width"], meta["height"] = img.size
    img.close()
    return meta


# ---------------------------------------------------------------------------
# DB insertion
# ---------------------------------------------------------------------------

def insert_image(conn: sqlite3.Connection, filepath: Path, meta: dict) -> bool:
    """Insert a single image into the open transaction. Does NOT commit."""
    path_str = str(filepath.resolve())
    image_hash = meta.get("hash", "")

    if conn.execute("SELECT id FROM images WHERE hash = ?", (image_hash,)).fetchone():
        return False  # duplicate content already indexed

    model_id = None
    if meta.get("model"):
        model_id = get_or_create(conn, "models", meta["model"])

    cur = conn.execute(
        """INSERT INTO images
           (filepath, filename, hash, width, height, source_ui, model_id,
            prompt, negative, seed, steps, sampler, cfg, rating, raw_meta)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            path_str, filepath.name, image_hash,
            meta.get("width"), meta.get("height"),
            meta.get("source_ui"), model_id,
            meta.get("prompt"), meta.get("negative"),
            meta.get("seed"), meta.get("steps"),
            meta.get("sampler"), meta.get("cfg"),
            meta.get("rating"), meta.get("raw"),
        ),
    )
    image_id = cur.lastrowid

    for lora in meta.get("loras", []):
        lora_id = get_or_create(conn, "loras", lora["name"])
        conn.execute(
            "INSERT OR IGNORE INTO image_loras (image_id, lora_id, weight) VALUES (?,?,?)",
            (image_id, lora_id, lora.get("weight")),
        )

    return True


def _flush_batch(conn: sqlite3.Connection, batch: list) -> int:
    """Insert a batch of (filepath, meta) tuples in a single transaction. Returns count of new images."""
    if not batch:
        return 0
    conn.execute("BEGIN")
    new_count = 0
    for filepath, meta in batch:
        if insert_image(conn, filepath, meta):
            new_count += 1
    conn.commit()
    return new_count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape AI image metadata into SQLite.")
    parser.add_argument("directory", help="Root directory to scan")
    parser.add_argument("--db", default="images.db", help="SQLite output file (default: images.db)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not write to DB")
    parser.add_argument("--verbose", action="store_true", help="Print details for every image")
    args = parser.parse_args()

    root = Path(args.directory)
    if not root.is_dir():
        print(f"Error: {root} is not a directory.")
        sys.exit(1)

    conn = init_db(args.db)
    files = sorted(root.rglob("*.png"))

    stats = {"total": 0, "new": 0, "skipped": 0, "unknown": 0,
             "swarmui": 0, "forge": 0, "comfyui": 0}

    print(f"Scanning {len(files)} PNG files in {root} ...\n")

    batch = []
    for filepath in files:
        stats["total"] += 1

        # Hash first — skip parsing if already indexed
        image_hash = compute_hash(filepath)
        if not args.dry_run and conn.execute("SELECT id FROM images WHERE hash = ?", (image_hash,)).fetchone():
            stats["skipped"] += 1
            continue

        meta = parse_image(filepath)
        if meta is None:
            stats["skipped"] += 1
            continue

        meta["hash"] = image_hash
        ui = meta.get("source_ui", "unknown")
        stats[ui] = stats.get(ui, 0) + 1

        batch.append((filepath, meta))

        if args.verbose or args.dry_run:
            prefix = "[DRY] " if args.dry_run else ""
            print(f"{prefix}{filepath.name}")
            print(f"  UI:     {ui}")
            print(f"  Model:  {meta.get('model') or '—'}")
            print(f"  LoRAs:  {[(l['name'], l['weight']) for l in meta.get('loras', [])] or '—'}")
            print(f"  Rating: {meta.get('rating') or '—'}")
            print(f"  Seed:   {meta.get('seed') or '—'}")
            print()

        # Batch commit
        if len(batch) >= BATCH_SIZE:
            if args.dry_run:
                stats["new"] += len(batch)
            else:
                stats["new"] += _flush_batch(conn, batch)
            batch = []

    # Final batch
    if batch:
        if args.dry_run:
            stats["new"] += len(batch)
        else:
            stats["new"] += _flush_batch(conn, batch)

    # Summary
    print("=" * 50)
    print(f"Done {'(dry run)' if args.dry_run else ''}")
    print(f"  Total files:  {stats['total']}")
    print(f"  Newly indexed:{stats['new']}")
    print(f"  Skipped:      {stats['skipped']}")
    print(f"  By UI:")
    print(f"    SwarmUI:    {stats.get('swarmui', 0)}")
    print(f"    Forge/A1111:{stats.get('forge', 0)}")
    print(f"    ComfyUI:    {stats.get('comfyui', 0)}")
    print(f"    Unknown:    {stats.get('unknown', 0)}")
    if not args.dry_run:
        print(f"\n  Database: {args.db}")

    # Useful starter queries
    if not args.dry_run:
        print("""
Example queries (run with: sqlite3 images.db):

-- Most used LoRAs
SELECT l.name, COUNT(*) AS uses, ROUND(AVG(il.weight),2) AS avg_weight
FROM image_loras il JOIN loras l ON l.id = il.lora_id
GROUP BY l.id ORDER BY uses DESC LIMIT 20;

-- Top rated model+lora combos (min 5 images)
SELECT m.name AS model, l.name AS lora,
       ROUND(AVG(i.rating),2) AS avg_rating, COUNT(*) AS count
FROM images i
JOIN models m ON m.id = i.model_id
JOIN image_loras il ON il.image_id = i.id
JOIN loras l ON l.id = il.lora_id
WHERE i.rating IS NOT NULL
GROUP BY m.id, l.id HAVING count >= 5
ORDER BY avg_rating DESC LIMIT 20;

-- Images with no LoRAs detected
SELECT filename, source_ui FROM images
WHERE id NOT IN (SELECT DISTINCT image_id FROM image_loras);
""")

    conn.close()


if __name__ == "__main__":
    main()
