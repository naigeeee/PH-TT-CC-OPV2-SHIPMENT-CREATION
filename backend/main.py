"""
Substrait app — serves both its web page and its API.

Substrait requires three things of this file:
  1. the server listens on port 8000          (set in cicd/Dockerfile.backend)
  2. GET /health returns 200                  (Substrait's readiness check)
  3. the JSON API lives under /api            (Substrait routes /api here)

Because this project has no frontend/ folder, Substrait sends ALL traffic to this
backend — including "/" — so this file also serves the page you see in the browser.
"""

import asyncio
import csv
import io
import random
import re
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

try:
    import openpyxl
except ImportError:
    openpyxl = None

APP_NAME = "OPV2 EML / Skywin / VF Vinflair Assign-Parcels Generator"

app = FastAPI(title=APP_NAME, docs_url="/api/docs")

jobs = {}
jobs_lock = asyncio.Lock()
chunk_store = {}
chunk_store_lock = asyncio.Lock()

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    # Ensure all unhandled errors return JSON, not HTML, so frontend shows readable message
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {exc}"})

@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}

@app.get("/api/info")
def info():
    return {
        "app": APP_NAME,
        "server_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

# ---------- helpers ----------

def _to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # Excel stores large parcel numbers as float e.g. 635003310968.0
        # Keep integer representation without scientific notation
        if v.is_integer():
            return str(int(v))
        # otherwise keep as is but remove trailing zeros
        s = str(v)
        return s
    if isinstance(v, int):
        return str(v)
    return str(v).strip()

def _clean_parcel(raw: str) -> str:
    s = _to_str(raw).strip()
    # remove whitespace, handle already JT prefix
    if not s:
        return s
    # if starts with JT then keep as is
    if s.upper().startswith("JT"):
        return s
    # if starts with 6, add JT
    if s.startswith("6"):
        return "JT" + s
    return s

def _extract_mawb_from_filename(filename: str) -> str:
    """Derive MAWB identifier from uploaded filename.
    Priority: explicit MAWB token, then 3-4+7-8 digit AWB pattern, then 10-11 digit run, else sanitized stem.
    Returns a safe stem without extension.
    """
    if not filename:
        return "EML"
    # strip path and extension
    base = filename.split("/")[-1].split("\\")[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    stem = stem.strip()
    if not stem:
        return "EML"
    # 1. MAWB token e.g. MAWB12345678, MAWB_123-45678901, MAWB: 618-12345678
    m = re.search(r"MAWB\D*([A-Z0-9\-]{6,20})", stem, re.I)
    if m:
        candidate = m.group(1).strip("-_ ").upper()
        candidate = re.sub(r"[^A-Z0-9\-]", "", candidate)
        if len(candidate) >= 6:
            return candidate
    # 2. Standard AWB pattern 3-4 digits, optional separator, 7-8 digits  e.g. 618-12345678 or 61812345678
    m = re.search(r"(?<!\d)(\d{3,4}[- ]?\d{7,8})(?!\d)", stem)
    if m:
        cand = m.group(1).replace(" ", "-").strip("- ")
        # normalize dash
        cand = re.sub(r"\s+", "-", cand)
        return cand
    # 3. Long numeric run 10-11 digits (common MAWB without dash)
    m = re.search(r"(?<!\d)(\d{10,11})(?!\d)", stem)
    if m:
        return m.group(1)
    # 4. Fallback: sanitized stem (keep alphanumeric, dash, underscore)
    safe = re.sub(r"[^\w\-]+", "_", stem).strip("_")
    safe = re.sub(r"_+", "_", safe)
    if len(safe) > 60:
        safe = safe[:60].rstrip("_")
    return safe or "EML"

def _safe_csv_filename(mawb: str) -> str:
    # ensure .csv extension and no path separators / quotes
    safe = re.sub(r'[^\w\-\.]+', '_', mawb).strip('_')
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    # strip any remaining unsafe chars for header
    safe = safe.replace('"', '').replace("'", "").replace("\n", "").replace("\r", "")
    return safe

def _parse_weight(v) -> Optional[float]:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    try:
        s = str(v).strip().replace(",", "")
        # handle empty
        if s == "":
            return None
        return float(s)
    except:
        return None

def _row_vals(row):
    return [str(c.value).strip().lower() if c.value is not None else "" for c in row]

def _iter_rows_range(ws, min_row, max_row, max_col=20):
    # Works for both normal and read_only worksheets; limit columns for 235-col files (37k rows)
    try:
        for row in ws.iter_rows(min_row=min_row, max_row=max_row, max_col=max_col, values_only=False):
            yield row
    except Exception:
        for r in range(min_row, max_row + 1):
            try:
                yield ws[r][:max_col] if hasattr(ws[r], "__iter__") else ws[r]
            except Exception:
                continue

def _get_header_vals(ws, header_row):
    # Return lower-cased vals for the header row, handling both sheet types
    for r, row in enumerate(_iter_rows_range(ws, header_row, header_row), start=header_row):
        if r == header_row:
            return _row_vals(row)
    return []

def _find_header_row(ws, required_keywords):
    """Scan first 20 rows for a header containing required keywords.
    required_keywords: list of list e.g. [["parcel"], ["bag"]]
    Returns row index (1-based) or None
    """
    best = None
    best_score = -1
    for r, row in enumerate(_iter_rows_range(ws, 1, 20), start=1):
        vals = _row_vals(row)
        score = 0
        match_all = True
        for kw_group in required_keywords:
            found = any(any(kw in v for kw in kw_group) for v in vals)
            if found:
                score += 1
            else:
                match_all = False
        if match_all and score > best_score:
            best = r
            best_score = score
    return best

def _find_skywin_header_row(ws):
    """Scan first 20 rows for carton-based header (Skywin/VF/JT): LM Tracking/Carton Number/Weight etc."""
    for r, row in enumerate(_iter_rows_range(ws, 1, 20), start=1):
        vals = _row_vals(row)
        # parcel indicator: lm+tracking, or tracking+number, or parcel, or jt
        has_parcel = any(
            ("lm" in v and "tracking" in v) or ("tracking" in v and ("no" in v or "number" in v)) or "parcel" in v or v.strip().startswith("jt")
            for v in vals
        )
        has_carton = any("carton" in v or "bag" in v for v in vals)
        if has_parcel and has_carton:
            return r
        has_carton_no = any("carton" in v and ("no" in v or "number" in v) for v in vals)
        if has_parcel and has_carton_no:
            return r
    return None

def _map_columns(ws, header_row, keywords_map):
    """keywords_map: dict col_name -> list of keywords (any must appear)
       Returns dict col_name -> index (0-based) or None
    """
    vals = _get_header_vals(ws, header_row)
    result = {}
    for name, kws in keywords_map.items():
        idx = None
        for i, v in enumerate(vals):
            if any(kw in v for kw in kws):
                idx = i
                break
        result[name] = idx
    return result

def _extract_skywin_parcels_from_wb(wb):
    """Extract Skywin parcels: Bag=Carton No (col F), Parcel=LM Tracking (col B), Weight=Carton Weight (col L). Also handles VF A/B/T."""
    parcels = []
    for ws in wb.worksheets:
        ws_start = len(parcels)
        header = _find_skywin_header_row(ws)
        parcel_col = bag_col = weight_col = None
        data_start = None
        if header is not None:
            vals = _get_header_vals(ws, header)
            # Parcel: LM Tracking / Tracking Number / Parcel No.
            for i, v in enumerate(vals):
                if ("lm" in v and "tracking" in v) or ("tracking" in v and ("no" in v or "number" in v)) or "parcel" in v:
                    parcel_col = i
                    break
            # also fallback for JT column
            if parcel_col is None:
                for i, v in enumerate(vals):
                    if v.strip().startswith("jt") or "lm" in v:
                        parcel_col = i
                        break
            # Bag: Carton No/Number or Bag ID
            for i, v in enumerate(vals):
                if ("carton" in v and ("no" in v or "number" in v) and "weight" not in v) or ("bag" in v and "id" in v):
                    bag_col = i
                    break
            if bag_col is None:
                for i, v in enumerate(vals):
                    if "carton" in v and "weight" not in v:
                        bag_col = i
                        break
            # Weight: prefer Carton Weight, then any weight (Parcel Weight)
            for i, v in enumerate(vals):
                if "carton" in v and "weight" in v:
                    weight_col = i
                    break
            if weight_col is None:
                for i, v in enumerate(vals):
                    if "weight" in v:
                        weight_col = i
                        break
            data_start = header + 1
        if header is not None and parcel_col is not None and bag_col is not None:
            max_needed = max(parcel_col, bag_col, weight_col if weight_col is not None else 0) + 1
            max_col_needed = min(max_needed + 5, 20)
            for row in ws.iter_rows(min_row=data_start, max_col=max_col_needed, values_only=True):
                if row is None:
                    continue
                if len(row) <= max(parcel_col, bag_col):
                    continue
                p = row[parcel_col]
                b = row[bag_col]
                w = None
                if weight_col is not None and len(row) > weight_col:
                    w = row[weight_col]
                if b is None or _to_str(b).strip() == "":
                    continue
                if p is None or _to_str(p).strip() == "":
                    continue
                ps = _to_str(p).strip()
                bs = _to_str(b).strip()
                if ps.lower().startswith("lm") and "tracking" in ps.lower():
                    continue
                if bs.lower().startswith("carton"):
                    continue
                # filter header-like
                if "tracking" in ps.lower() and len(ps) < 20:
                    # might be header row re-check
                    if ps.lower().startswith("lm"):
                        continue
                parcels.append({"parcel": ps, "bag": bs, "weight": _parse_weight(w)})
            if len(parcels) > ws_start:
                continue
        # fallback positional: Skywin B=1/F=5/L=11, VF A=0/B=1/T=19, and template A/B/C
        fallback_found = []
        for min_row in [4, 2, 1]:
            tmp = []
            for row in ws.iter_rows(min_row=min_row, max_col=20, values_only=True):
                if row is None:
                    continue
                # try Skywin B/F/L
                if len(row) > 11:
                    p = row[1]  # B LM Tracking
                    b = row[5]  # F Carton No
                    w = row[11] # L Carton Weight
                    if p is not None and b is not None and _to_str(p).strip() != "" and _to_str(b).strip() != "":
                        bs = _to_str(b).strip()
                        ps = _to_str(p).strip()
                        if not (ps.lower().startswith("lm") or bs.lower().startswith("carton")) and len(ps) >= 5 and len(bs) >= 5:
                            # validate Skywin bag pattern-ish (not header)
                            if "tracking" not in ps.lower() or len(ps) > 20:
                                tmp.append({"parcel": ps, "bag": bs, "weight": _parse_weight(w)})
                                continue
                # try VF A/B/T  (A=LM Tracking, B=Carton Number, T=Carton Weight col 19)
                if len(row) > 19:
                    p = row[0]  # A
                    b = row[1]  # B
                    w = row[19] # T
                    if p is not None and b is not None and _to_str(p).strip() != "" and _to_str(b).strip() != "":
                        bs = _to_str(b).strip()
                        ps = _to_str(p).strip()
                        if not (ps.lower().startswith("lm") or bs.lower().startswith("carton")) and len(ps) >= 5 and len(bs) >= 5:
                            # avoid duplicate if already added via Skywin path
                            exists = any(x["bag"] == bs and x["parcel"] == ps for x in tmp)
                            if not exists and ("tracking" not in ps.lower() or len(ps) > 20):
                                # VF bag is WS01OB... or similar, ensure weight valid
                                wp = _parse_weight(w)
                                if wp is not None:
                                    tmp.append({"parcel": ps, "bag": bs, "weight": wp})
                                    continue
                # also handle template copy where A=LM Tracking, B=Carton No/Number, C=Weight (pasted from template)
                if len(row) > 2:
                    p2 = row[0]
                    b2 = row[1]
                    w2 = row[2]
                    if p2 is not None and b2 is not None and _to_str(p2).strip() != "" and _to_str(b2).strip() != "":
                        bs2 = _to_str(b2).strip()
                        ps2 = _to_str(p2).strip()
                        if len(ps2) >= 5 and len(bs2) >= 5 and not ps2.lower().startswith("lm"):
                            if "tracking" not in ps2.lower() or len(ps2) > 20:
                                exists = any(x["bag"] == bs2 and x["parcel"] == ps2 for x in tmp)
                                if not exists:
                                    tmp.append({"parcel": ps2, "bag": bs2, "weight": _parse_weight(w2)})
            if tmp:
                fallback_found = tmp
                break
        if fallback_found:
            parcels.extend(fallback_found)
        # generic carton scan if still none for this sheet (handles 828 file with A/B/D layout)
        if not fallback_found:
            gen_tmp = []
            for row in ws.iter_rows(min_row=2, max_col=20, values_only=True):
                if row is None:
                    continue
                # quick skip header row
                row_str = " ".join([_to_str(v).lower() for v in row[:5] if v is not None])
                if "lm" in row_str and "tracking" in row_str and "carton" in row_str:
                    continue
                bag_idx = parcel_idx = None
                for idx, val in enumerate(row):
                    s = _to_str(val).strip()
                    if not s:
                        continue
                    if s.upper().startswith("WNJPH") or s.upper().startswith("JT") or (s.startswith("635") and len(s) >= 8) or (s.isdigit() and len(s) >= 10):
                        if parcel_idx is None:
                            parcel_idx = idx
                    if (s.startswith("00000") or s.upper().startswith("WS01OB") or s.upper().startswith("TT") or "OB202" in s.upper() or s.upper().startswith("OB")) and len(s) >= 8:
                        bag_idx = idx
                if parcel_idx is not None and bag_idx is not None:
                    p = row[parcel_idx]
                    b = row[bag_idx]
                    ps = _to_str(p).strip()
                    bs = _to_str(b).strip()
                    if not ps or not bs or len(ps) < 5 or len(bs) < 8:
                        continue
                    # find weight near bag/parcel or in common columns D(3), H(7), L(11), T(19)
                    w = None
                    for wi in [bag_idx + 1, bag_idx + 2, 3, 7, 11, 19]:
                        if 0 <= wi < len(row):
                            pw = _parse_weight(row[wi])
                            if pw is not None and 0 < pw < 1000:
                                # avoid picking parcel number as weight (parcel numbers are huge)
                                if pw < 500:  # carton weights are typically < 100
                                    w = pw
                                    break
                    # fallback: any plausible weight in row
                    if w is None:
                        for val in row:
                            pw = _parse_weight(val)
                            if pw is not None and 0.01 <= pw <= 500 and abs(pw - 15.5) < 20:
                                # heuristic: prefer 15.5-like carton weight or small parcel weight
                                w = pw
                                break
                    gen_tmp.append({"parcel": ps, "bag": bs, "weight": w})
                    if len(gen_tmp) >= 100000:
                        break
            if gen_tmp:
                parcels.extend(gen_tmp)
    return parcels

def _normalize_carrier(carrier: str) -> str:
    c = (carrier or "auto").lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if c in ("vf", "vinflair", "vfvinflair"):
        return "vf"
    if c == "skywin":
        return "skywin"
    if c == "eml":
        return "eml"
    return "auto"

def _extract_parcels_from_wb_unified(wb, carrier="auto"):
    """Unified extractor: tries Skywin/VF and EML based on carrier."""
    carrier = _normalize_carrier(carrier)
    carton_parcels = []  # Skywin + VF share LM Tracking / Carton logic
    eml_parcels = []
    if carrier in ("auto", "skywin", "vf"):
        carton_parcels = _extract_skywin_parcels_from_wb(wb)  # now handles both Skywin and VF
        if carrier in ("skywin", "vf"):
            return carton_parcels
        # auto: if carton parcels found and look valid, return early to avoid double scan on large files
        if carrier == "auto" and carton_parcels:
            if len(carton_parcels) >= 2 and any(p.get("weight") is not None for p in carton_parcels[:5]):
                return carton_parcels
            # also if bag pattern looks like carton (00000/WS) return
            if any("B202" in p["bag"] or p["bag"].startswith("00000") for p in carton_parcels[:5]):
                return carton_parcels
    if carrier in ("auto", "eml"):
        eml_parcels = _extract_eml_parcels_from_wb(wb)
        if carrier == "eml":
            return eml_parcels
    # auto: prefer the type that yielded more rows
    if carrier == "auto":
        if carton_parcels and eml_parcels:
            # Skywin/VF cartons are often 00000B... or WS01..., EML bags are WS01...
            # Use scoring: carton bags often contain 00000 or B202, but both can be WS01...
            # So prefer the set with more rows or with weights
            carton_score = sum(1 for p in carton_parcels if p["bag"].startswith("00000") or "B202" in p["bag"] or p["bag"].upper().startswith("WS"))
            eml_score = sum(1 for p in eml_parcels if p["bag"].upper().startswith("WS"))
            # also weight presence
            carton_w = sum(1 for p in carton_parcels if p.get("weight") is not None)
            eml_w = sum(1 for p in eml_parcels if p.get("weight") is not None)
            if carton_score > eml_score or carton_w > eml_w:
                return carton_parcels
            if eml_score > carton_score or eml_w > carton_w:
                return eml_parcels
            return carton_parcels if len(carton_parcels) >= len(eml_parcels) else eml_parcels
        return carton_parcels if carton_parcels else eml_parcels
    return eml_parcels or carton_parcels

def _extract_eml_parcels_from_wb(wb):
    parcels = []
    for ws in wb.worksheets:
        ws_start = len(parcels)
        # try header detection
        header = _find_header_row(ws, [["parcel"], ["bag"]])
        parcel_col = bag_col = weight_col = None
        data_start = None
        if header is not None:
            mapping = _map_columns(ws, header, {
                "parcel": ["parcel"],
                "bag": ["bag"],
                "weight": ["weight", "carton"]
            })
            # refine parcel vs bag: header row values
            vals = _get_header_vals(ws, header)
            # Need to disambiguate parcel and bag columns which both contain "bag"/"parcel" sometimes
            # Re-map more precisely
            # Find parcel column: must contain parcel
            for i, v in enumerate(vals):
                if "parcel" in v and ("no" in v or "id" in v or "number" in v):
                    parcel_col = i
                    break
            if parcel_col is None:
                parcel_col = mapping.get("parcel")
            # bag column: must contain bag and id
            for i, v in enumerate(vals):
                if "bag" in v and "id" in v:
                    bag_col = i
                    break
            if bag_col is None:
                # fallback to any bag
                for i, v in enumerate(vals):
                    if "bag" in v:
                        bag_col = i
                        break
                if bag_col is None:
                    bag_col = mapping.get("bag")
            # weight column
            for i, v in enumerate(vals):
                if "weight" in v:
                    weight_col = i
                    break
            if weight_col is None:
                weight_col = mapping.get("weight")
            # ensure parcel and bag are different
            if parcel_col is not None and bag_col is not None and parcel_col == bag_col:
                # try to find second candidate
                bag_col = None
                for i, v in enumerate(vals):
                    if "bag" in v and i != parcel_col:
                        bag_col = i
                        break
            data_start = header + 1
        if header is not None and parcel_col is not None and bag_col is not None:
            max_needed = max(parcel_col, bag_col, weight_col if weight_col is not None else 0) + 1
            max_col_needed = min(max_needed + 5, 20)
            for row in ws.iter_rows(min_row=data_start, max_col=max_col_needed, values_only=True):
                if row is None:
                    continue
                # ensure length
                if len(row) <= max(parcel_col, bag_col):
                    continue
                p = row[parcel_col]
                b = row[bag_col]
                w = None
                if weight_col is not None and len(row) > weight_col:
                    w = row[weight_col]
                if b is None or _to_str(b).strip() == "":
                    continue
                if p is None or _to_str(p).strip() == "":
                    continue
                ps = _to_str(p).strip()
                bs = _to_str(b).strip()
                # filter header-like rows
                if ps.lower().startswith("parcel"):
                    continue
                if bs.lower().startswith("bag"):
                    continue
                # skip rows where bag looks not like bag id but still allow
                parcels.append({"parcel": ps, "bag": bs, "weight": _parse_weight(w)})
            if len(parcels) > ws_start:
                # continue to next sheet to collect all, but don't fallback
                continue
        # fallback: try fixed columns G=6, H=7 (EML main manifest) and C=2 weight
        # Also try A=0, B=1 (template left side)
        fallback_found = []
        # Try reading from row 4 onward as template suggests
        for min_row in [4, 2, 1]:
            tmp = []
            for row in ws.iter_rows(min_row=min_row, max_col=20, values_only=True):
                if row is None:
                    continue
                # try G/H
                p = None
                b = None
                w = None
                if len(row) > 7:
                    p = row[6]
                    b = row[7]
                if p is not None and b is not None and _to_str(p).strip() != "" and _to_str(b).strip() != "":
                    # check if b looks like bag id (WS)
                    if "WS" in _to_str(b).upper() or _to_str(b).upper().startswith("WS"):
                        if _to_str(p).lower().startswith("parcel"):
                            continue
                        w_val = None
                        if len(row) > 2:
                            w_val = _parse_weight(row[2])
                        tmp.append({"parcel": _to_str(p).strip(), "bag": _to_str(b).strip(), "weight": w_val})
                        continue
                # try A/B
                if len(row) > 1:
                    p2 = row[0]
                    b2 = row[1]
                    if p2 is not None and b2 is not None and _to_str(p2).strip() != "" and _to_str(b2).strip() != "":
                        if "WS" in _to_str(b2).upper():
                            if _to_str(p2).lower().startswith("parcel"):
                                continue
                            w_val = None
                            if len(row) > 2:
                                w_val = _parse_weight(row[2])
                            # avoid duplicate if already captured via G/H
                            # check not already added with same bag+parcel
                            exists = any(x["bag"] == _to_str(b2).strip() and x["parcel"] == _to_str(p2).strip() for x in tmp)
                            if not exists:
                                tmp.append({"parcel": _to_str(p2).strip(), "bag": _to_str(b2).strip(), "weight": w_val})
            if len(tmp) >= 1:
                # also check that we have plausible data (at least one bag with WS)
                fallback_found = tmp
                break
        if fallback_found:
            parcels.extend(fallback_found)
        # if still none, brute force scan for any row with WS bag id
        if not parcels:
            # generic scan: look for any two columns where one looks like bag (WS) and other plausible parcel
            # scan first 20 columns
            for row in ws.iter_rows(max_col=20, values_only=True):
                if row is None:
                    continue
                # find bag column candidate
                bag_idx = None
                for i, v in enumerate(row):
                    s = _to_str(v)
                    if s.upper().startswith("WS") and len(s) > 8:
                        bag_idx = i
                        break
                if bag_idx is not None:
                    # parcel likely left of bag or nearby
                    parcel_idx = bag_idx - 1 if bag_idx > 0 else None
                    if parcel_idx is not None:
                        p = row[parcel_idx]
                        b = row[bag_idx]
                        if p is not None and _to_str(p).strip() != "" and not _to_str(p).lower().startswith("parcel"):
                            # try weight: bag weight may be in column nearby
                            w = None
                            # check immediate right columns for weight numeric
                            for wi in range(bag_idx+1, min(len(row), bag_idx+4)):
                                pw = _parse_weight(row[wi])
                                if pw is not None and 0 < pw < 1000:
                                    w = pw
                                    break
                            # avoid adding header rows
                            if _to_str(p).strip().lower() in ("parcel no.", "parcel no", "parcel"):
                                continue
                            parcels.append({"parcel": _to_str(p).strip(), "bag": _to_str(b).strip(), "weight": w})
    # dedup? keep all but group later
    return parcels

# backward-compat alias used by older calls; defaults to auto-detect EML/Skywin
def _extract_parcels_from_wb(wb, carrier="auto"):
    return _extract_parcels_from_wb_unified(wb, carrier)

def _extract_bag_weights_from_wb(wb):
    mapping = {}
    for ws in wb.worksheets:
        header = _find_header_row(ws, [["bag"], ["weight"]])
        bag_col = weight_col = None
        data_start = None
        if header is not None:
            vals = _get_header_vals(ws, header)
            for i, v in enumerate(vals):
                if "bag" in v and "id" in v:
                    bag_col = i
                    break
            if bag_col is None:
                for i, v in enumerate(vals):
                    if "bag" in v:
                        bag_col = i
                        break
            for i, v in enumerate(vals):
                if "weight" in v:
                    weight_col = i
                    break
            # Also handle bagID / bagWEIGHT naming
            if bag_col is None:
                for i, v in enumerate(vals):
                    if v.replace(" ", "") == "bagid" or "bagid" in v.replace(" ", ""):
                        bag_col = i
                        break
            if weight_col is None:
                for i, v in enumerate(vals):
                    if "bagweight" in v.replace(" ", "") or "carton" in v:
                        weight_col = i
                        break
            data_start = header + 1
        if header is not None and bag_col is not None and weight_col is not None:
            max_needed_b = max(bag_col, weight_col) + 1
            for row in ws.iter_rows(min_row=data_start, max_col=min(max_needed_b + 5, 20), values_only=True):
                if row is None or len(row) <= max(bag_col, weight_col):
                    continue
                b = row[bag_col]
                w = row[weight_col]
                if b is None or _to_str(b).strip() == "":
                    continue
                bs = _to_str(b).strip()
                if bs.lower().startswith("bag"):
                    continue
                wp = _parse_weight(w)
                if wp is not None:
                    mapping[bs] = wp
            if mapping:
                continue
        # fallback: try fixed columns B=1, F=5 (bag list) and J=9, K=10 (template)
        # Try B/F
        tmp = {}
        for min_row in [4, 2, 1]:
            cand = {}
            for row in ws.iter_rows(min_row=min_row, values_only=True):
                if row is None:
                    continue
                # B/F
                if len(row) > 5:
                    b = row[1]
                    w = row[5]
                    if b is not None and w is not None and _to_str(b).strip() != "":
                        if _to_str(b).lower().startswith("bag"):
                            continue
                        wp = _parse_weight(w)
                        if wp is not None and ("WS" in _to_str(b).upper()):
                            cand[_to_str(b).strip()] = wp
                # J/K (9,10)
                if len(row) > 10:
                    b2 = row[9]
                    w2 = row[10]
                    if b2 is not None and w2 is not None and _to_str(b2).strip() != "":
                        if _to_str(b2).lower().startswith("bag"):
                            continue
                        wp2 = _parse_weight(w2)
                        if wp2 is not None and ("WS" in _to_str(b2).upper()):
                            cand[_to_str(b2).strip()] = wp2
            if cand:
                mapping.update(cand)
                break
        # generic scan: any bag id followed by weight
        if not mapping:
            for row in ws.iter_rows(max_col=20, values_only=True):
                if row is None:
                    continue
                for i, v in enumerate(row):
                    s = _to_str(v)
                    if s.upper().startswith("WS") and len(s) > 8:
                        # look ahead for weight
                        for wi in range(i+1, min(len(row), i+6)):
                            wp = _parse_weight(row[wi])
                            if wp is not None and 0 < wp < 10000:
                                # ensure not a parcel id numeric
                                # weight is plausible (0.1 - 100)
                                if wp < 1000:
                                    mapping[s] = wp
                                    break
                        break
    return mapping

def _csv_rows(content: bytes):
    """Decode uploaded CSV bytes and parse all rows.

    newline='' is REQUIRED (per csv docs): manifest address cells often contain
    embedded line breaks, which SheetJS quotes on export. Without it the parse
    fails with 'new-line character seen in unquoted field'. utf-8-sig strips a
    BOM if the browser added one.
    """
    text = content.decode("utf-8-sig", errors="ignore")
    try:
        # Sniff on a sample with line breaks flattened: on CRLF files Sniffer
        # otherwise mistakes '\r' for the delimiter and every row misparses.
        sample = text[:2048].replace("\r\n", "\n").replace("\r", "\n")
        dialect = csv.Sniffer().sniff(sample)
        if dialect.delimiter not in (",", ";", "\t", "|"):
            dialect = csv.excel
    except:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text, newline=''), dialect))

def _extract_skywin_parcels_from_csv(content: bytes):
    rows = _csv_rows(content)
    parcels = []
    header_idx = None
    parcel_col = bag_col = weight_col = None
    for idx, row in enumerate(rows[:20]):
        lowers = [c.strip().lower() for c in row]
        has_lm = any("lm" in c and "tracking" in c for c in lowers)
        has_carton = any("carton" in c for c in lowers)
        if has_lm and has_carton:
            header_idx = idx
            for i, v in enumerate(lowers):
                if "lm" in v and "tracking" in v and parcel_col is None:
                    parcel_col = i
                if "carton" in v and ("no" in v or "number" in v) and "weight" not in v and bag_col is None:
                    bag_col = i
                if "carton" in v and "weight" in v and weight_col is None:
                    weight_col = i
            if weight_col is None:
                for i, v in enumerate(lowers):
                    if "weight" in v and weight_col is None:
                        weight_col = i
            break
    if header_idx is not None and parcel_col is not None and bag_col is not None:
        for row in rows[header_idx+1:]:
            if len(row) <= max(parcel_col, bag_col):
                continue
            p = row[parcel_col].strip()
            b = row[bag_col].strip()
            if not p or not b:
                continue
            if p.lower().startswith("lm") or b.lower().startswith("carton"):
                continue
            w = None
            if weight_col is not None and len(row) > weight_col:
                w = _parse_weight(row[weight_col])
            parcels.append({"parcel": p, "bag": b, "weight": w})
        return parcels
    return []

def _extract_parcels_from_csv(content: bytes, carrier="auto"):
    # try carton-based (Skywin/VF) first if auto/skywin/vf
    carrier = _normalize_carrier(carrier)
    if carrier in ("auto", "skywin", "vf"):
        sky = _extract_skywin_parcels_from_csv(content)
        if sky:
            if carrier in ("skywin", "vf"):
                return sky
            pass
        elif carrier in ("skywin", "vf"):
            return []
    # EML path
    rows = _csv_rows(content)
    parcels = []
    # find header row
    header_idx = None
    parcel_col = bag_col = weight_col = None
    for idx, row in enumerate(rows[:20]):
        lowers = [c.strip().lower() for c in row]
        has_parcel = any("parcel" in c for c in lowers)
        has_bag = any("bag" in c for c in lowers)
        if has_parcel and has_bag:
            header_idx = idx
            for i, v in enumerate(lowers):
                if "parcel" in v and parcel_col is None:
                    parcel_col = i
                if "bag" in v and "id" in v and bag_col is None:
                    bag_col = i
                if "weight" in v and weight_col is None:
                    weight_col = i
            if parcel_col is None:
                for i, v in enumerate(lowers):
                    if "parcel" in v:
                        parcel_col = i
                        break
            if bag_col is None:
                for i, v in enumerate(lowers):
                    if "bag" in v:
                        bag_col = i
                        break
            break
    if header_idx is not None and parcel_col is not None and bag_col is not None:
        for row in rows[header_idx+1:]:
            if len(row) <= max(parcel_col, bag_col):
                continue
            p = row[parcel_col].strip()
            b = row[bag_col].strip()
            if not p or not b:
                continue
            if p.lower().startswith("parcel") or b.lower().startswith("bag"):
                continue
            w = None
            if weight_col is not None and len(row) > weight_col:
                w = _parse_weight(row[weight_col])
            parcels.append({"parcel": p, "bag": b, "weight": w})
    else:
        # fallback: assume column 0=parcel,1=bag maybe
        for row in rows[1:]:
            if len(row) < 2:
                continue
            p = row[0].strip()
            b = row[1].strip()
            if not p or not b:
                continue
            w = _parse_weight(row[2]) if len(row) > 2 else None
            if "WS" in b.upper():
                parcels.append({"parcel": p, "bag": b, "weight": w})
    if carrier == "auto" and 'sky' in locals() and sky:
        # choose the larger set
        if len(sky) > len(parcels):
            return sky
        # also if sky has weights and eml has none, prefer sky
        sky_weights = sum(1 for p in sky if p.get("weight") is not None)
        eml_weights = sum(1 for p in parcels if p.get("weight") is not None)
        if sky_weights > eml_weights:
            return sky
    return parcels

def _extract_bag_weights_from_csv(content: bytes):
    rows = _csv_rows(content)
    mapping = {}
    header_idx = None
    bag_col = weight_col = None
    for idx, row in enumerate(rows[:20]):
        lowers = [c.strip().lower() for c in row]
        has_bag = any("bag" in c for c in lowers)
        has_weight = any("weight" in c for c in lowers)
        if has_bag and has_weight:
            header_idx = idx
            for i, v in enumerate(lowers):
                if "bag" in v and bag_col is None:
                    bag_col = i
                if "weight" in v and weight_col is None:
                    weight_col = i
            break
    if header_idx is not None and bag_col is not None and weight_col is not None:
        for row in rows[header_idx+1:]:
            if len(row) <= max(bag_col, weight_col):
                continue
            b = row[bag_col].strip()
            w = _parse_weight(row[weight_col])
            if b and w is not None:
                mapping[b] = w
    return mapping

def _debug_headers(content: bytes, filename: str) -> str:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        parts = []
        for ws in wb.worksheets[:2]:
            try:
                for r, row in enumerate(_iter_rows_range(ws, 1, 3), start=1):
                    vals = [str(c.value) if c.value is not None else "" for c in row[:6]]
                    parts.append(f"{ws.title}!R{r}={vals}")
                    if r >= 2:
                        break
            except Exception:
                continue
            if parts:
                break
        wb.close()
        return " | ".join(parts)[:500] if parts else "no sheet data"
    except Exception as e:
        return f"debug failed: {e}"

def _load_workbook_from_bytes(content: bytes, filename: str):
    if openpyxl is None:
        raise HTTPException(status_code=500, detail="openpyxl not installed")
    # Use read_only for >2 MB to avoid OOM and for wide files (235 cols) — with max_col=20 extraction is now fast
    use_read_only = len(content) > 2 * 1024 * 1024
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=use_read_only)
        return wb
    except Exception as e:
        # Fallback try other mode
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=not use_read_only)
            return wb
        except Exception as e2:
            raise HTTPException(status_code=400, detail=f"Failed to read Excel file '{filename}': {e2}")

def _extract_via_pandas(content: bytes, filename: str, carrier: str):
    """Fast path for large wide files (37k x 235) — read only first 20 cols via pandas, auto-convert to efficient in-memory table."""
    try:
        import pandas as pd
    except ImportError:
        raise HTTPException(status_code=500, detail="pandas not installed")
    try:
        # usecols=range(20) limits to first 20 columns as requested (keeps all 37k rows)
        # Use openpyxl engine for xlsx, pyxlsb for xlsb
        lower = filename.lower()
        engine = "pyxlsb" if lower.endswith(".xlsb") else "openpyxl"
        # For xlsb, open with pyxlsb; for xlsx, openpyxl
        try:
            df = pd.read_excel(io.BytesIO(content), engine=engine, usecols=range(20), nrows=None)
        except Exception as e:
            if "out-of-bounds" in str(e) or "Out-of-bounds" in str(e):
                df = pd.read_excel(io.BytesIO(content), engine=engine, nrows=None)
            else:
                raise
        # pandas may have read header as first row; ensure we have header
        # Normalize column names
        cols = [str(c).strip().lower() if c is not None else "" for c in df.columns]
        # Detect header mapping similar to openpyxl logic
        # Find parcel/bag/weight columns by header names
        parcel_col = bag_col = weight_col = None
        for i, c in enumerate(cols):
            if ("lm" in c and "tracking" in c) or ("tracking" in c and ("no" in c or "number" in c)) or "parcel" in c:
                if parcel_col is None:
                    parcel_col = i
            if ("carton" in c and ("no" in c or "number" in c) and "weight" not in c) or ("bag" in c and "id" in c):
                if bag_col is None:
                    bag_col = i
            if "carton" in c and "weight" in c:
                if weight_col is None:
                    weight_col = i
        if weight_col is None:
            for i, c in enumerate(cols):
                if "weight" in c and weight_col is None:
                    weight_col = i
        # If header not found, try fallback positions: VF A/B/T, Skywin B/F/L, EML G/H etc.
        # For pandas, positions are 0-indexed within usecols 0-19, so A=0, B=1, D=3, F=5, L=11, T=19
        # If not found, try to infer from data patterns later
        # Now extract rows
        parcels = []
        # Also handle case where header detection failed — use pandas' data as is, try to find pattern per row
        if parcel_col is None or bag_col is None:
            # fallback: try positional as per carrier
            # For auto, try all
            pass
        # Iterate rows
        for _, row in df.iterrows():
            # row is Series with up to 20 cols
            vals = row.tolist()
            # skip empty
            if all(pd.isna(v) for v in vals):
                continue
            p = vals[parcel_col] if parcel_col is not None and parcel_col < len(vals) else None
            b = vals[bag_col] if bag_col is not None and bag_col < len(vals) else None
            w = vals[weight_col] if weight_col is not None and weight_col < len(vals) else None
            # if header mapping failed, try generic pattern per row
            if (p is None or str(p).strip() == "") or (b is None or str(b).strip() == ""):
                # try generic: find parcel/bag by pattern in this row's first 20 cols
                for idx, val in enumerate(vals):
                    s = str(val).strip() if not pd.isna(val) else ""
                    if not s:
                        continue
                    if s.upper().startswith("WNJPH") or s.upper().startswith("JT") or (s.startswith("635") and len(s) >= 8):
                        if p is None or str(p).strip() == "":
                            p = val
                    if s.startswith("00000") or s.upper().startswith("WS01OB") or s.upper().startswith("TT") or "OB202" in s.upper():
                        if b is None or str(b).strip() == "":
                            b = val
                # try weight near bag
                if w is None or (isinstance(w, float) and pd.isna(w)):
                    for val in vals:
                        pw = _parse_weight(val)
                        if pw is not None and 0 < pw < 500:
                            w = val
                            break
            if b is None or str(b).strip() == "":
                continue
            if p is None or str(p).strip() == "":
                continue
            ps = str(p).strip()
            bs = str(b).strip()
            if ps.lower().startswith("lm") and "tracking" in ps.lower():
                continue
            if bs.lower().startswith("carton"):
                continue
            parcels.append({"parcel": ps, "bag": bs, "weight": _parse_weight(w)})
        return parcels
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Pandas fast path failed for '{filename}': {e}")

# ID validation is SHAPE-based, not an allowlist: real Bag/Parcel IDs are pure
# alphanumeric, 6-30 chars, containing at least one digit (e.g. 00000OB…, TT…,
# WS…, XGFJOB… bags; WNJPH…/JT…/PT… parcels). Anything else in those columns
# (address fragments, phone numbers, notes like 'gate with "The Baker" tarp
# before Pratesi') is junk and must be excluded. A strict prefix allowlist was
# tried first and wrongly dropped the legitimate XGFJOB bag series, so new
# vendor prefixes are kept as long as they are well-formed IDs.
BAG_PREFIXES = ("00000OB", "0000OB", "OB", "TT", "WS", "XGFJOB")
PARCEL_PREFIXES = ("WNJPH", "JT", "PT")

def _looks_like_id(s) -> bool:
    """Shape check: pure alphanumeric, 6-30 chars, contains at least one digit.
    Rejects address/phone/notes junk (spaces, commas, +, quotes, dots)."""
    if s is None:
        return False
    t = _to_str(s).strip()
    if not (6 <= len(t) <= 30):
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+", t):
        return False
    return any(ch.isdigit() for ch in t)

def _is_valid_bag(bag) -> bool:
    return _looks_like_id(bag)

def _is_valid_parcel(parcel) -> bool:
    return _looks_like_id(parcel)

def _group_and_select(parcels, bag_weights):
    """Group parcels by bag, pick 1 parcel per bag. Returns
    (rows, excluded_count, excluded_samples). Rows whose Bag or Parcel fails ID
    validation (weird prefix / junk text) are excluded, not output."""
    # group by bag
    grouped = OrderedDict()
    for p in parcels:
        bag = p["bag"]
        # ensure bag string normalized
        bag = _to_str(bag).strip()
        if bag not in grouped:
            grouped[bag] = []
        grouped[bag].append(p)
    result = []
    excluded = 0
    excluded_samples = []
    for bag, plist in grouped.items():
        if not _is_valid_bag(bag):
            excluded += len(plist)
            if len(excluded_samples) < 5:
                excluded_samples.append(f"Bag={str(bag)[:40]}")
            continue
        # WNJPH rule: if bag contains any WNJPH-prefix parcel, bag = parcel (e.g., 0000OB202608313643)
        has_wnjph = any(_to_str(p.get("parcel", "")).strip().upper().startswith("WNJPH") for p in plist)
        if has_wnjph:
            parcel = bag
            # still need chosen for weight fallback
            chosen = plist[0]
        else:
            # prefer valid parcels; random pick among those (1 bag = 1 parcel)
            valid_cands = [p for p in plist if _is_valid_parcel(_clean_parcel(_to_str(p.get("parcel", "")).strip()))]
            if not valid_cands:
                excluded += len(plist)
                if len(excluded_samples) < 5:
                    excluded_samples.append(f"Bag={str(bag)[:40]} Parcel={_to_str(plist[0].get('parcel', ''))[:40]}")
                continue
            if len(valid_cands) < len(plist) and len(excluded_samples) < 5:
                bad = next(p for p in plist if p not in valid_cands)
                excluded_samples.append(f"Parcel={_to_str(bad.get('parcel', ''))[:40]}")
            excluded += len(plist) - len(valid_cands)
            chosen = random.choice(valid_cands) if len(valid_cands) > 1 else valid_cands[0]
            raw_parcel = _to_str(chosen["parcel"]).strip()
            parcel = _clean_parcel(raw_parcel)
        if not _is_valid_parcel(parcel):
            excluded += len(plist)
            if len(excluded_samples) < 5:
                excluded_samples.append(f"Bag={str(bag)[:40]} Parcel={str(parcel)[:40]}")
            continue
        # weight: prefer bag_weights mapping, else chosen weight, else any parcel weight in group
        weight = bag_weights.get(bag)
        if weight is None:
            weight = chosen.get("weight")
            if weight is None:
                # try any in group
                for cand in plist:
                    if cand.get("weight") is not None:
                        weight = cand.get("weight")
                        break
        if weight is None:
            # if still none, set 0 or skip? We'll set empty but include row with 0
            weight = 0
        # ensure float formatting: keep as original but normalize
        # keep weight as float, frontend will format
        result.append({
            "Bag": bag,
            "Parcel": parcel,
            "Manifest Weight": weight,
            "Bag Weight": weight
        })
    # sort by Bag for stable output? keep insertion order which is appearance order
    return result, excluded, excluded_samples

def _excluded_warning(excluded, excluded_samples):
    if not excluded:
        return None
    samples = "; ".join(excluded_samples[:5])
    return (f"{excluded} parcel row(s) with unrecognized Bag/Parcel IDs were auto-excluded "
            f"(IDs must be alphanumeric with digits - e.g. bags 00000OB/TT/WS/XGFJOB, parcels WNJPH/JT/PT)"
            + (f", e.g. {samples}" if samples else ""))

@app.post("/api/generate")
async def generate(
    file: Optional[UploadFile] = File(None, description="Excel manifest file (single, backward compat)"),
    files: Optional[List[UploadFile]] = File(None, description="Parcel manifest files, up to 3 (EML/Skywin/VF)"),
    bag_file: Optional[UploadFile] = File(None, description="Optional bag list file with Bag ID & Weight (required for EML)"),
    carrier: str = Form("auto", description="Carrier: auto, eml, skywin, vf"),
):
    # collect manifest files (support up to 3 files, either via 'file' or 'files')
    manifest_list: List[UploadFile] = []
    if files:
        manifest_list.extend(files)
    if file is not None and file.filename:
        manifest_list.append(file)
    if not manifest_list:
        raise HTTPException(status_code=400, detail="No manifest file uploaded. Attach 1-3 parcel manifest files.")
    if len(manifest_list) > 3:
        raise HTTPException(status_code=400, detail=f"Too many manifest files: {len(manifest_list)} uploaded, max 3 allowed.")
    carrier = _normalize_carrier(carrier)
    parcels = []
    bag_weights = {}
    # Use first filename for MAWB display; also handle .csv vs .xlsx merging
    first_filename = manifest_list[0].filename or "upload.xlsx"
    first_content_debug = None
    # Process each manifest file and merge parcels/bag_weights
    for mf in manifest_list:
        content = await mf.read()
        if first_content_debug is None:
            first_content_debug = content
        if not content:
            continue
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"Manifest file '{mf.filename}' too large: {len(content)/(1024*1024):.1f} MB, max 50 MB")
        fname = mf.filename or "upload.xlsx"
        lower = fname.lower()
        is_csv = lower.endswith(".csv")
        try:
            if is_csv:
                p = _extract_parcels_from_csv(content, carrier)
                bw = _extract_bag_weights_from_csv(content)
            else:
                # Auto-convert Excel to CSV-like handling for large files to avoid 504 (first 20 cols, all rows)
                # This is the requested XLSB→CSV switch: treat Excel as efficient CSV in-memory
                if len(content) > 1 * 1024 * 1024:
                    try:
                        p = _extract_via_pandas(content, fname, carrier)
                        bw = {}
                        if not p:
                            raise ValueError("pandas found no parcels")
                    except Exception:
                        wb = _load_workbook_from_bytes(content, fname)
                        p = _extract_parcels_from_wb(wb, carrier)
                        bw = _extract_bag_weights_from_wb(wb)
                else:
                    wb = _load_workbook_from_bytes(content, fname)
                    p = _extract_parcels_from_wb(wb, carrier)
                    bw = _extract_bag_weights_from_wb(wb)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to read manifest file '{fname}': {e}")
        parcels.extend(p)
        bag_weights.update(bw)
    filename = first_filename
    try:
        # if bag_file provided, merge its weights
        if bag_file is not None and bag_file.filename:
            try:
                bcontent = await bag_file.read()
                if bcontent:
                    # size guard
                    if len(bcontent) > 50 * 1024 * 1024:
                        raise HTTPException(status_code=400, detail=f"Bag file too large: {len(bcontent)/(1024*1024):.1f} MB, max 50 MB")
                    blower = (bag_file.filename or "").lower()
                    if blower.endswith(".csv"):
                        extra = _extract_bag_weights_from_csv(bcontent)
                        bag_weights.update(extra)
                    else:
                        bwb = _load_workbook_from_bytes(bcontent, bag_file.filename)
                        extra = _extract_bag_weights_from_wb(bwb)
                        bag_weights.update(extra)
                        extra_parcels = _extract_parcels_from_wb(bwb)
                        if not parcels and extra_parcels:
                            parcels = extra_parcels
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to read bag file: {e}")
        if not parcels:
            dbg = ""
            if first_content_debug:
                try:
                    dbg = " First rows: " + _debug_headers(first_content_debug, first_filename)
                except Exception:
                    pass
            if carrier == "skywin":
                detail = "No parcels found. Ensure Skywin file has columns LM Tracking (col B), Carton No (col F) & Carton Weight (col L). Checked first 20 rows, also tried B/F/L fallback." + dbg
            elif carrier == "eml":
                detail = "No parcels found. Ensure EML file has columns Parcel No (col G) and Parcel Bag ID (col H). Checked first 20 rows, also tried G/H and A/B fallbacks." + dbg
            elif carrier == "vf":
                detail = "No parcels found. Ensure VF Vinflair file has columns LM Tracking (col A), Carton Number (col B) & Carton Weight (col T). Checked first 20 rows, also tried A/B/T fallback." + dbg
            else:
                detail = "No parcels found. Ensure file has EML columns (Parcel No/Bag ID), Skywin (LM Tracking/Carton No/Carton Weight) or VF (LM Tracking/Carton Number/Carton Weight). Checked first 20 rows." + dbg
            raise HTTPException(status_code=400, detail=detail)
        # group (invalid Bag/Parcel IDs auto-excluded)
        rows, excluded, excluded_samples = _group_and_select(parcels, bag_weights)
        # detect missing weights
        missing = sum(1 for r in rows if r["Bag Weight"] == 0)
        # derive MAWB-based filename from uploaded names (parcel manifest preferred, else bag list)
        mawb = _extract_mawb_from_filename(filename)
        if bag_file is not None and bag_file.filename:
            bag_mawb = _extract_mawb_from_filename(bag_file.filename)
            if re.search(r"\d{3}[- ]?\d{7,8}|\d{10,11}", bag_mawb) and not re.search(r"\d{3}[- ]?\d{7,8}|\d{10,11}", mawb):
                mawb = bag_mawb
            if mawb.lower() in ("upload", "eml", "file", "manifest") and bag_mawb.lower() not in ("upload", "eml"):
                mawb = bag_mawb
        csv_filename = _safe_csv_filename(mawb)
        warnings = []
        excl = _excluded_warning(excluded, excluded_samples)
        if excl:
            warnings.append(excl)
        if missing:
            warnings.append(f"{missing} bag(s) had no weight found — set to 0. Upload a bag list file (Bag ID in column B, Carton Weight in column F) if available.")
        return JSONResponse({
            "count": len(rows),
            "rows": rows,
            "warnings": warnings,
            "parcels_raw": len(parcels),
            "bags_unique": len(rows),
            "excluded": excluded,
            "mawb": mawb,
            "filename": csv_filename,
            "carrier": carrier
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")

@app.post("/api/generate/csv")
async def generate_csv(
    file: Optional[UploadFile] = File(None, description="Excel manifest file (single, backward compat)"),
    files: Optional[List[UploadFile]] = File(None, description="Parcel manifest files, up to 3"),
    bag_file: Optional[UploadFile] = File(None),
    carrier: str = Form("auto")
):
    # reuse logic but return CSV file — supports up to 3 manifest files
    manifest_list: List[UploadFile] = []
    if files:
        manifest_list.extend(files)
    if file is not None and file.filename:
        manifest_list.append(file)
    if not manifest_list:
        raise HTTPException(status_code=400, detail="No manifest file uploaded.")
    if len(manifest_list) > 3:
        raise HTTPException(status_code=400, detail=f"Too many manifest files: {len(manifest_list)}, max 3 allowed.")
    carrier = _normalize_carrier(carrier)
    parcels = []
    bag_weights = {}
    first_filename = manifest_list[0].filename or "upload.xlsx"
    for mf in manifest_list:
        content = await mf.read()
        if not content:
            continue
        if len(content) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"Manifest file '{mf.filename}' too large: {len(content)/(1024*1024):.1f} MB, max 50 MB")
        fname = mf.filename or "upload.xlsx"
        lower = fname.lower()
        is_csv = lower.endswith(".csv")
        try:
            if is_csv:
                p = _extract_parcels_from_csv(content, carrier)
                bw = _extract_bag_weights_from_csv(content)
            else:
                if len(content) > 1 * 1024 * 1024:
                    try:
                        p = _extract_via_pandas(content, fname, carrier)
                        bw = {}
                        if not p:
                            raise ValueError("pandas found no parcels")
                    except Exception:
                        wb = _load_workbook_from_bytes(content, fname)
                        p = _extract_parcels_from_wb(wb, carrier)
                        bw = _extract_bag_weights_from_wb(wb)
                else:
                    wb = _load_workbook_from_bytes(content, fname)
                    p = _extract_parcels_from_wb(wb, carrier)
                    bw = _extract_bag_weights_from_wb(wb)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to read manifest file '{fname}': {e}")
        parcels.extend(p)
        bag_weights.update(bw)
    filename = first_filename
    try:
        if bag_file is not None and bag_file.filename:
            bcontent = await bag_file.read()
            if bcontent:
                if len(bcontent) > 50 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail=f"Bag file too large: {len(bcontent)/(1024*1024):.1f} MB")
                blower = (bag_file.filename or "").lower()
                if blower.endswith(".csv"):
                    bag_weights.update(_extract_bag_weights_from_csv(bcontent))
                else:
                    bwb = _load_workbook_from_bytes(bcontent, bag_file.filename)
                    bag_weights.update(_extract_bag_weights_from_wb(bwb))
        if not parcels:
            raise HTTPException(status_code=400, detail="No parcels found.")
        rows, excluded, excluded_samples = _group_and_select(parcels, bag_weights)
        # MAWB-based filename (parcel manifest preferred)
        mawb = _extract_mawb_from_filename(filename)
        if bag_file is not None and bag_file.filename:
            bag_mawb = _extract_mawb_from_filename(bag_file.filename)
            if re.search(r"\d{3}[- ]?\d{7,8}|\d{10,11}", bag_mawb) and not re.search(r"\d{3}[- ]?\d{7,8}|\d{10,11}", mawb):
                mawb = bag_mawb
            if mawb.lower() in ("upload", "eml", "file", "manifest") and bag_mawb.lower() not in ("upload", "eml"):
                mawb = bag_mawb
        csv_filename = _safe_csv_filename(mawb)
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["Bag", "Parcel", "Manifest Weight", "Bag Weight"])
        for r in rows:
            writer.writerow([r["Bag"], r["Parcel"], r["Manifest Weight"], r["Bag Weight"]])
        csv_bytes = output.getvalue().encode("utf-8")
        from fastapi.responses import Response
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{csv_filename}"'}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")

# Option A: Chunked + gzip upload for large files (100k rows, 21 MB) — 5 MB chunks to stay under 60s/40m gateway
@app.post("/api/upload/chunk")
async def upload_chunk(
    chunk: UploadFile = File(...),
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    filename: str = Form(...),
    is_gzipped: bool = Form(False),
):
    # Store chunk in memory (up to 40 MB total, 5 MB per chunk)
    if chunk_index < 0 or total_chunks <= 0 or total_chunks > 50:
        raise HTTPException(status_code=400, detail="Invalid chunk index/total (max 50 chunks, use 1 MB chunks for 40 MB file)")
    content = await chunk.read()
    if is_gzipped:
        try:
            import gzip
            content = gzip.decompress(content)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Gzip decompress failed: {e}")
    if len(content) > 40 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Chunk too large")
    async with chunk_store_lock:
        if upload_id not in chunk_store:
            chunk_store[upload_id] = {"filename": filename, "total": total_chunks, "chunks": {}, "created": time.time()}
        # Cleanup old uploads >1 hour
        now = time.time()
        for uid in list(chunk_store.keys()):
            if now - chunk_store[uid]["created"] > 3600:
                del chunk_store[uid]
        store = chunk_store[upload_id]
        store["chunks"][chunk_index] = content
        received = len(store["chunks"])
    return {"upload_id": upload_id, "received": received, "total": total_chunks, "chunk_index": chunk_index}

@app.post("/api/upload/complete")
async def upload_complete(
    upload_id: str = Form(...),
    filename: str = Form(...),
    carrier: str = Form("auto"),
    total_chunks: int = Form(...),
):
    async with chunk_store_lock:
        store = chunk_store.get(upload_id)
        if not store:
            raise HTTPException(status_code=404, detail="Upload not found")
        if len(store["chunks"]) != total_chunks:
            raise HTTPException(status_code=400, detail=f"Missing chunks: {len(store['chunks'])}/{total_chunks}")
        # Reassemble in order
        parts = [store["chunks"][i] for i in range(total_chunks)]
        content = b"".join(parts)
        # Cleanup
        del chunk_store[upload_id]
    # Now process as if it were a single upload (first 20 cols, all rows)
    lower = filename.lower()
    is_csv = lower.endswith(".csv")
    carrier = _normalize_carrier(carrier)
    parcels = []
    bag_weights = {}
    try:
        if is_csv:
            parcels = _extract_parcels_from_csv(content, carrier)
            bag_weights = _extract_bag_weights_from_csv(content)
        else:
            # Use pandas fast path for large (like 37k rows) to avoid 504
            if len(content) > 1 * 1024 * 1024:
                try:
                    parcels = _extract_via_pandas(content, filename, carrier)
                    bag_weights = {}
                    if not parcels:
                        raise ValueError("pandas empty")
                except Exception:
                    wb = _load_workbook_from_bytes(content, filename)
                    parcels = _extract_parcels_from_wb(wb, carrier)
                    bag_weights = _extract_bag_weights_from_wb(wb)
            else:
                wb = _load_workbook_from_bytes(content, filename)
                parcels = _extract_parcels_from_wb(wb, carrier)
                bag_weights = _extract_bag_weights_from_wb(wb)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read reassembled file '{filename}': {e}")
    if not parcels:
        raise HTTPException(status_code=400, detail="No parcels found after reassembling chunks")
    rows, excluded, excluded_samples = _group_and_select(parcels, bag_weights)
    mawb = _extract_mawb_from_filename(filename)
    csv_filename = _safe_csv_filename(mawb)
    warnings = []
    excl = _excluded_warning(excluded, excluded_samples)
    if excl:
        warnings.append(excl)
    return JSONResponse({
        "count": len(rows),
        "rows": rows,  # full rows — frontend previews first 500 but downloads full CSV from this data
        "warnings": warnings,
        "parcels_raw": len(parcels),
        "bags_unique": len(rows),
        "excluded": excluded,
        "mawb": mawb,
        "filename": csv_filename,
        "carrier": carrier,
        "full_count": len(rows),
    })

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP_NAME__</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: linear-gradient(180deg, #f6f8fb 0%, #eef2f7 100%);
    color: #172033;
    padding: 24px;
  }
  .wrap { max-width: 1100px; margin: 0 auto; }
  .head { text-align: center; margin-bottom: 18px; }
  .eyebrow { display:inline-block; padding:6px 12px; border-radius:999px; background:#e7eefc; color:#33508f; font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
  .head h1 { margin:8px 0 4px; font-size:26px; letter-spacing:-.02em; color:#0f172a; }
  .head p { margin:0; color:#526076; font-size:14px; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:16px; padding:20px; box-shadow: 0 10px 30px rgba(15,23,42,.06); margin-bottom:16px; }
  .card h2 { margin:0 0 12px; font-size:15px; color:#0f172a; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  @media (max-width:800px){ .grid2{grid-template-columns:1fr} }
  .drop { border:1.5px dashed #cbd5e1; border-radius:12px; padding:16px; background:#f8fafc; text-align:center; transition: border-color .15s, background .15s; }
  .drop.dragover { border-color:#2f5fd0; background:#eef4ff; }
  .drop input[type=file] { display:block; margin:10px auto 0; }
  .drop .hint { font-size:12px; color:#64748b; margin-top:6px; }
  .btn { display:inline-flex; align-items:center; gap:8px; padding:10px 18px; border-radius:10px; border:0; font-weight:700; font-size:14px; cursor:pointer; transition: filter .15s, opacity .15s; }
  .btn-primary { background:#2f5fd0; color:#fff; }
  .btn-primary:disabled { opacity:.5; cursor:not-allowed; }
  .btn-ghost { background:#eef2ff; color:#2f5fd0; }
  .btn-sm { padding:7px 12px; font-size:13px; }
  .row-actions { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }
  .meta { font-size:13px; color:#475569; }
  .alert { padding:10px 12px; border-radius:10px; font-size:13px; margin-top:12px; }
  .alert-ok { background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; }
  .alert-warn { background:#fffbeb; border:1px solid #fcd34d; color:#92400e; }
  .alert-err { background:#fef2f2; border:1px solid #fecaca; color:#991b1b; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:#64748b; border-bottom:1px solid #e2e8f0; padding:8px 10px; background:#f8fafc; position:sticky; top:0; }
  td { padding:8px 10px; border-bottom:1px solid #f1f5f9; }
  tr:hover td { background:#f8faff; }
  .table-wrap { max-height: 420px; overflow:auto; border:1px solid #e2e8f0; border-radius:12px; }
  .mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  ul.instr { margin:8px 0 0; padding-left:18px; color:#475569; }
  ul.instr li { margin:4px 0; }
  code { background:#f1f5f9; padding:1px 5px; border-radius:6px; font-size:12px; }
  .foot { text-align:center; font-size:12px; color:#94a3b8; margin-top:10px; }
  .progress { height:8px; background:#e2e8f0; border-radius:999px; overflow:hidden; margin-top:10px; display:none; }
  .progress > div { height:100%; width:0%; background:#2f5fd0; transition:width .2s; }
  .modal-backdrop { position:fixed; inset:0; background:rgba(15,23,42,.55); display:none; align-items:center; justify-content:center; z-index:9999; padding:20px; }
  .modal-backdrop.show { display:flex; }
  .modal { background:#fff; border-radius:16px; max-width:520px; width:100%; padding:24px; box-shadow:0 20px 60px rgba(0,0,0,.25); text-align:center; }
  .modal h3 { margin:0 0 8px; font-size:18px; color:#0f172a; }
  .modal p { margin:8px 0; color:#475569; font-size:14px; }
  .modal .big { font-size:40px; }
  .countdown { font-weight:800; color:#2f5fd0; }
</style>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/pako@2.1.0/dist/pako.min.js"></script>
</head>
<body>
  <div class="wrap">
     <header class="head">
      <span class="eyebrow">PH TT CC • OPV2 Shipment Creation</span>
      <h1>EML / Skywin / VF Vinflair Assign-Parcels Generator</h1>
      <p>Upload your EML, Skywin or VF Vinflair Excel manifest and get the 4-column <strong>Bag / Parcel / Manifest Weight / Bag Weight</strong> CSV in one click.</p>
    </header>

    <div class="card">
      <h2>How it works — replaces the manual Google Sheet steps</h2>
      <div class="grid2">
        <div>
          <strong>Before (manual):</strong>
          <ul class="instr">
            <li><strong>EML:</strong> Copy <code>Parcel No. (col G)</code> + <code>Parcel Bag ID (col H)</code> from EML manifest → paste into <code>A &amp; B</code>. Copy <code>Bag ID (col B)</code> + <code>Carton Weight (col F)</code> from bag list → paste into <code>J &amp; K</code>. Click <em>Refresh EML Data</em>.</li>
            <li><strong>Skywin:</strong> Copy <code>LM Tracking (col B)</code> + <code>Carton No (col F)</code> + <code>Carton Weight (col L)</code> from Skywin manifest → paste into <code>A-C</code>. Click <em>Refresh Skywin Data</em>.</li>
            <li><strong>VF Vinflair:</strong> Copy <code>LM Tracking (col A)</code> + <code>Carton Number (col B)</code> + <code>Carton Weight (col T)</code> from VF Vinflair manifest → paste into <code>A-C</code>. Click <em>Refresh VF Data</em>.</li>
            <li>Formula generates <code>assign-parcels-template</code> sheet (Bag, Parcel, Manifest Weight, Bag Weight).</li>
          </ul>
        </div>
        <div>
          <strong>Now (this app):</strong>
          <ul class="instr">
            <li>Select carrier below (or Auto-detect) and upload the same Excel file(s) — the app auto-detects columns (EML: G/H, B/F or <code>Parcel No./Bag ID/Carton Weight</code>; Skywin: B/F/L or <code>LM Tracking/Carton No/Carton Weight</code>; VF Vinflair: A/B/T or <code>LM Tracking/Carton Number/Carton Weight</code>).</li>
            <li>Logic: 1 bag = 1 parcel. If a bag has multiple parcels, a <em>random</em> parcel is picked. Weights are taken from the bag/carton weight (Manifest Weight = Bag Weight). Rows whose Bag/Parcel don't look like IDs (address or phone junk — IDs are alphanumeric with digits, e.g. 00000OB/TT/WS/XGFJOB bags, WNJPH/JT/PT parcels) are auto-excluded and listed in the warnings.</li>
            <li>If parcel starts with <code>6</code>, it is auto-prefixed to <code>JT6…</code> (e.g. <code>635003310968</code> → <code>JT635003310968</code>).</li>
            <li>Click <strong>Generate</strong> → preview &amp; download CSV ready for OPV2 upload. Up to 3 manifest files merged.</li>
          </ul>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>1) Upload files</h2>
      <div style="margin-bottom:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap">
        <label for="carrier" style="font-weight:700; font-size:13px">Carrier:</label>
        <select id="carrier" style="padding:7px 12px; border:1px solid #cbd5e1; border-radius:8px; background:#fff; font-weight:600">
          <option value="auto" selected>Auto-detect (EML / Skywin / VF)</option>
          <option value="eml">EML</option>
          <option value="skywin">Skywin — B/F/L</option>
          <option value="vf">VF Vinflair — A/B/T</option>
        </select>
        <span id="carrierHint" class="hint"></span>
      </div>
      <div class="grid2">
        <label class="drop" id="drop1">
          <strong id="label1">Parcel Manifest file(s) * — up to 3</strong><br>
          <span class="hint" id="hint1">EML: Parcel No (col G) &amp; Bag ID (col H) · Skywin: LM Tracking (col B) + Carton No (col F) + Carton Weight (col L) · VF Vinflair: LM Tracking (col A) + Carton Number (col B) + Carton Weight (col T). Accepts .xlsx, .xls, .csv — select 1-3 files.</span>
          <input id="file1" type="file" accept=".xlsx,.xls,.csv" multiple>
          <div id="fname1" class="hint"></div>
          <button id="clear1" type="button" style="display:none; margin-top:8px; padding:6px 12px; border-radius:8px; border:1px solid #e2e8f0; background:#fff; color:#64748b; font-size:12px; font-weight:600; cursor:pointer">✕ Remove manifest file(s)</button>
        </label>
        <label class="drop" id="drop2">
          <strong id="label2">Bag List file (EML only)</strong><br>
          <span class="hint" id="hint2">EML bag weight list — Bag ID (col B) &amp; Carton Weight (col F). Skywin / VF Vinflair do NOT need this file.</span>
          <input id="file2" type="file" accept=".xlsx,.xls,.csv">
          <div id="fname2" class="hint"></div>
          <button id="clear2" type="button" style="display:none; margin-top:8px; padding:6px 12px; border-radius:8px; border:1px solid #e2e8f0; background:#fff; color:#64748b; font-size:12px; font-weight:600; cursor:pointer">✕ Remove bag list file</button>
        </label>
      </div>
      <div class="hint" style="margin-top:10px" id="tip">Tip: Auto-detect works for EML, Skywin and VF Vinflair. Skywin/VF need 1 file (LM Tracking + Carton + Weight); EML needs 2 files (manifest + bag list). You may upload up to 3 manifest files at once — they will be merged (useful when EML/Skywin/VF split a shipment across 3 files).</div>
      <div class="row-actions">
        <button id="gen" class="btn btn-primary">Generate</button>
        <button id="dl" class="btn btn-ghost" disabled>Download CSV</button>
        <span id="meta" class="meta"></span>
      </div>
      <div class="progress" id="prog"><div id="progBar"></div></div>
      <div id="msg"></div>
    </div>
  <div class="modal-backdrop" id="modalBg">
    <div class="modal">
      <div class="big" id="modalIcon">⚠️</div>
      <h3 id="modalTitle">File too large</h3>
      <p id="modalBody">This file exceeds the server limit.</p>
      <p id="modalCount" class="countdown"></p>
      <div class="row-actions" style="justify-content:center">
        <button id="modalOk" class="btn btn-primary btn-sm">OK</button>
      </div>
    </div>
  </div>

    <div class="card" id="outCard" style="display:none">
      <h2>2) Result — <span id="rowCount"></span></h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Bag</th><th>Parcel</th><th>Manifest Weight</th><th>Bag Weight</th></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div class="foot">Columns match <code>(EML) / (Skywin) / (VF Vinflair) assign-parcels-template</code>: Bag | Parcel | Manifest Weight | Bag Weight — ready to upload to OPV2.</div>

    </div>
  </div>

<script>
const $ = s => document.querySelector(s);
const file1 = $('#file1'), file2 = $('#file2');
const fname1 = $('#fname1'), fname2 = $('#fname2');
const gen = $('#gen'), dl = $('#dl'), meta = $('#meta'), msg = $('#msg');
const outCard = $('#outCard'), tbody = $('#tbody'), rowCount = $('#rowCount');
const carrier = $('#carrier'), carrierHint = $('#carrierHint');
const label1 = $('#label1'), hint1 = $('#hint1'), label2 = $('#label2'), hint2 = $('#hint2'), tip = $('#tip');
const clear1 = $('#clear1'), clear2 = $('#clear2');
let lastRows = [];
let lastFilename = "EML_assign_parcels.csv";
// Server limits: nginx client_max_body_size 40m, per-file backend cap 50MB.
// Warn at 30MB (user-asked threshold), hard-block above 40MB to avoid 502/504 Bad Gateway.
const WARN_MB = 30, HARD_MB = 50;
const modalBg = () => document.querySelector('#modalBg');
function showModal(title, body, icon, autoRefreshSec){
  document.querySelector('#modalTitle').textContent = title;
  document.querySelector('#modalBody').innerHTML = body;
  document.querySelector('#modalIcon').textContent = icon || '⚠️';
  const bg = modalBg(); bg.classList.add('show');
  const cnt = document.querySelector('#modalCount');
  if(autoRefreshSec){
    let s = autoRefreshSec;
    cnt.textContent = `Refreshing page in ${s}s so the next upload can proceed…`;
    const iv = setInterval(()=>{
      s -= 1;
      if(s <= 0){ clearInterval(iv); location.reload(); }
      else cnt.textContent = `Refreshing page in ${s}s so the next upload can proceed…`;
    }, 1000);
  } else { cnt.textContent = ''; }
}
document.addEventListener('click', e=>{ if(e.target && e.target.id==='modalOk') modalBg().classList.remove('show'); });
function isGatewayErrorStatus(s){ return s===502||s===503||s===504||s===413||s===524; }
function isGatewayErrorText(t){
  if(!t) return false;
  t = String(t).toLowerCase();
  return t.includes('bad gateway')||t.includes('gateway timeout')||t.includes('504')||t.includes('502')||
    t.includes('request entity too large')||t.includes('payload too large')||t.includes('413')||
    t.includes('<html')||t.includes('nginx');
}
function handleGatewayFailure(where, status, text){
  const extra = status ? ` (HTTP ${status})` : '';
  showModal('Server limit reached — Bad Gateway'+extra,
    `${where} hit the server upload/timeout limit (max ~${HARD_MB} MB per file, 300s processing).<br>`+
    `Tip: split a large manifest into smaller files, or convert Excel → CSV before uploading — the app does this automatically for files over 5 MB.<br>`+
    `The page will auto-refresh so the next user can continue without manually fixing the error.`,
    '🚨', 2);
}
// Convert a large Excel manifest to CSV in the browser (SheetJS) and auto-split into
// smaller CSV parts so each upload stays under the gateway limit. Returns true if split.
async function autoSplitIfLarge(inputEl){
  try{
    if(!inputEl.files || inputEl.files.length!==1) return false;
    const f = inputEl.files[0];
    if(f.size <= 5*1024*1024) return false;
    if(typeof XLSX==='undefined'){ console.warn('XLSX lib missing, cannot auto-split'); return false; }
    const buf = await f.arrayBuffer();
    const wb = XLSX.read(buf, {type:'array', dense:true});
    // pick sheet with most rows
    let best = null, bestLen = 0;
    wb.SheetNames.forEach(n=>{
      const ws = wb.Sheets[n];
      const rows = XLSX.utils.sheet_to_json(ws, {header:1, raw:true, defval:''});
      if(rows.length > bestLen){ bestLen = rows.length; best = {name:n, rows}; }
    });
    if(!best || bestLen < 2) return false;
    // Strip embedded CR/LF from cells (multi-line addresses break CSV row
    // structure and crash server-side parsing). Bag/Parcel/Weight never
    // legitimately contain line breaks. Large integers become strings to
    // prevent sheet_to_csv from formatting them as scientific notation.
    const cleanCell = v => {
      if(typeof v==='string' && /[\\r\\n]/.test(v)) v = v.replace(/[\\r\\n]+/g, ' ');
      if(typeof v==='number' && Number.isInteger(v) && Math.abs(v) >= 100000) v = String(v);
      return v;
    };
    const header = best.rows[0].map(cleanCell);
    const data = best.rows.slice(1).map(r=>r.map(cleanCell)).filter(r=>r.some(v=>String(v).trim()!==''));
    if(!data.length) return false;
    // split data into up to 3 CSV parts (~equal rows, each well under 10MB)
    const PARTS = data.length > 20000 ? 3 : 2;
    const per = Math.ceil(data.length / PARTS);
    const base = f.name.replace(/\\.[^.]+$/,'');
    const dt = new DataTransfer();
    for(let i=0;i<PARTS;i++){
      const chunk = data.slice(i*per, (i+1)*per);
      if(!chunk.length) continue;
      const ws = XLSX.utils.aoa_to_sheet([header, ...chunk]);
      const csv = XLSX.utils.sheet_to_csv(ws);
      const nf = new File([csv], `${base}_part${i+1}of${PARTS}.csv`, {type:'text/csv'});
      dt.items.add(nf);
    }
    if(dt.files.length>1){
      inputEl.files = dt.files;
      inputEl.dispatchEvent(new Event('change'));
      return true;
    }
    return false;
  }catch(e){ console.warn('autoSplit failed', e); return false; }
}
async function convertExcelToCSV(inputEl){
  try{
    if(!inputEl.files || !inputEl.files.length) return false;
    if(typeof XLSX==='undefined') return false;
    const dt = new DataTransfer();
    let converted = 0;
    for(const f of Array.from(inputEl.files)){
      const nm = f.name.toLowerCase();
      const isExcel = nm.endsWith('.xlsx')||nm.endsWith('.xls')||nm.endsWith('.xlsb');
      if(!isExcel){ dt.items.add(f); continue; }
      const buf = await f.arrayBuffer();
      const wb = XLSX.read(buf, {type:'array', dense:true});
      let best = null, bestLen = 0;
      wb.SheetNames.forEach(n=>{
        const ws = wb.Sheets[n];
        const rows = XLSX.utils.sheet_to_json(ws, {header:1, raw:true, defval:''});
        if(rows.length > bestLen){ bestLen = rows.length; best = {name:n, rows}; }
      });
      if(!best || bestLen < 2){ dt.items.add(f); continue; }
      const cleanCell = v => {
        if(typeof v==='string' && /[\\r\\n]/.test(v)) v = v.replace(/[\\r\\n]+/g, ' ');
        // Large integers must become strings — sheet_to_csv formats them as
        // scientific notation (6.35E+11) which the server rejects as junk IDs.
        if(typeof v==='number' && Number.isInteger(v) && Math.abs(v) >= 100000) v = String(v);
        return v;
      };
      const header = best.rows[0].map(cleanCell);
      const data = best.rows.slice(1).map(r=>r.map(cleanCell)).filter(r=>r.some(v=>String(v).trim()!==''));
      if(!data.length){ dt.items.add(f); continue; }
      const ws = XLSX.utils.aoa_to_sheet([header, ...data]);
      const csv = XLSX.utils.sheet_to_csv(ws);
      const base = f.name.replace(/\.[^.]+$/,'');
      dt.items.add(new File([csv], base + '.csv', {type:'text/csv'}));
      converted++;
    }
    if(converted > 0){
      inputEl.files = dt.files;
      inputEl.dispatchEvent(new Event('change'));
    }
    return converted > 0;
  }catch(e){ console.warn('convertExcelToCSV failed', e); return false; }
}
function fmtMB(b){ return (b/1024/1024).toFixed(1)+' MB'; }
function checkSizesOrBlock(){
  // returns {blocked:boolean} — shows popup when any file exceeds HARD_MB
  const all = [...Array.from(file1.files||[]), ...Array.from(file2.files||[])];
  for(const f of all){
    if(f.size > HARD_MB*1024*1024){
      showModal('File exceeds server limit',
        `<code>${f.name}</code> is <strong>${fmtMB(f.size)}</strong> — server allows max ~${HARD_MB} MB per file (warns from ${WARN_MB} MB).<br>`+
        `Please split it into smaller files or convert Excel → CSV first, then re-upload. Remove the file with the ✕ button below the upload box.`,
        '🚫', 0);
      return {blocked:true};
    }
  }
  // soft warning
  const big = all.filter(f=>f.size > WARN_MB*1024*1024);
  if(big.length){
    msg.innerHTML = `<div class="alert alert-warn">⚠️ ${big.map(f=>`<code>${f.name}</code> (${fmtMB(f.size)})`).join(', ')} exceed${big.length>1?'':'s'} ${WARN_MB} MB — upload may hit Bad Gateway timeout. The app will auto-convert Excel → CSV and auto-split into smaller parts to help.</div>`;
  }
  return {blocked:false};
}
function updateProgress(pct){
  const p = document.querySelector('#prog'), b = document.querySelector('#progBar');
  if(pct==null){ p.style.display='none'; b.style.width='0%'; return; }
  p.style.display='block'; b.style.width = Math.min(100, Math.max(0, pct))+'%';
}
function updateCarrierUI(){
  const v = carrier.value;
  if(v==='skywin'){
    label1.textContent = 'Skywin Manifest file(s) * — up to 3';
    hint1.textContent = 'Skywin parcel manifest — LM Tracking (col B), Carton No (col F) & Carton Weight (col L). 1-3 files allowed.';
    label2.textContent = 'Bag List file (not needed)';
    hint2.textContent = 'Skywin does NOT need a bag list — cartons and weights are in the manifest itself.';
    document.getElementById('drop2').style.opacity = '0.55';
    carrierHint.textContent = 'Skywin: 1-3 files (B/F/L). Bag list is ignored if attached.';
    tip.textContent = 'Skywin: upload 1-3 files (LM Tracking + Carton No + Carton Weight). EML still needs bag list. VF uses A/B/T.';
  } else if(v==='vf'){
    label1.textContent = 'VF Vinflair Manifest file(s) * — up to 3';
    hint1.textContent = 'VF Vinflair manifest — LM Tracking (col A), Carton Number (col B) & Carton Weight (col T). 1-3 files allowed.';
    label2.textContent = 'Bag List file (not needed)';
    hint2.textContent = 'VF Vinflair does NOT need a bag list — cartons and weights are in the manifest itself.';
    document.getElementById('drop2').style.opacity = '0.55';
    carrierHint.textContent = 'VF Vinflair: 1-3 files (A/B/T). Bag list is ignored if attached.';
    tip.textContent = 'VF Vinflair: upload 1-3 files (LM Tracking col A + Carton Number col B + Carton Weight col T).';
  } else if(v==='eml'){
    label1.textContent = 'EML Parcel Manifest file(s) * — up to 3';
    hint1.textContent = 'EML main parcel manifest — Parcel No (col G) & Bag ID (col H). 1-3 files allowed. Accepts .xlsx, .xls, .csv';
    label2.textContent = 'Bag List file * (EML)';
    hint2.textContent = 'EML bag weight list — Bag ID (col B) & Carton Weight (col F). Required for EML.';
    document.getElementById('drop2').style.opacity = '1';
    carrierHint.textContent = 'EML: 1-3 manifest files + 1 bag list (B/F).';
    tip.textContent = 'EML: upload 1-3 manifests (G/H) + bag list (B/F). Skywin uses B/F/L, VF uses A/B/T, both 1-3 files and no bag list.';
  } else {
    label1.textContent = 'Parcel Manifest file(s) * — up to 3';
    hint1.textContent = 'EML: Parcel No (G) & Bag ID (H) · Skywin: LM Tracking (B) + Carton No (F) + Carton Weight (L) · VF Vinflair: LM Tracking (A) + Carton Number (B) + Carton Weight (T). Accepts .xlsx, .xls, .csv — select 1-3 files.';
    label2.textContent = 'Bag List file (EML only)';
    hint2.textContent = 'EML bag weight list — Bag ID (col B) & Carton Weight (col F). Skywin / VF Vinflair do NOT need this file.';
    document.getElementById('drop2').style.opacity = '1';
    carrierHint.textContent = 'Auto-detect will try EML (Parcel/Bag), Skywin (LM/Carton B/F/L) and VF (LM/Carton A/B/T).';
    tip.textContent = 'Auto-detect works for EML, Skywin and VF Vinflair. Skywin/VF need 1-3 files (LM Tracking + Carton + Weight); EML needs 1-3 manifests + bag list. Up to 3 manifests will be merged.';
  }
}
carrier.addEventListener('change', updateCarrierUI);
updateCarrierUI();
function extractMAWB(filename){
  if(!filename) return "EML";
  let base = filename.split('/').pop();
  let stem = base.includes('.') ? base.slice(0, base.lastIndexOf('.')) : base;
  stem = stem.trim();
  if(!stem) return "EML";
  let m = stem.match(/MAWB\\D*([A-Z0-9\\-]{6,20})/i);
  if(m){
    let cand = m[1].trim().replace(/^[-_ ]+|[-_ ]+$/g,'').toUpperCase().replace(/[^A-Z0-9\\-]/g,'');
    if(cand.length>=6) return cand;
  }
  m = stem.match(/(\\d{3,4}[- ]?\\d{7,8})/);
  if(m) return m[1].replace(/ /g,'-').trim();
  m = stem.match(/(\\d{10,11})/);
  if(m) return m[1];
  let safe = stem.replace(/[^\\w\\-]+/g,'_').replace(/^_+|_+$/g,'').replace(/_+/g,'_');
  if(safe.length>60) safe = safe.slice(0,60).replace(/_+$/,'');
  return safe || "EML";
}
function safeCsvFilename(mawb){
  let s = mawb.replace(/[^\\w\\-\\.]+/g,'_').replace(/^_+|_+$/g,'');
  if(!s.toLowerCase().endsWith('.csv')) s += '.csv';
  return s;
}
function currentMAWB(){
  let a = file1.files[0] ? extractMAWB(file1.files[0].name) : "";
  let b = file2.files[0] ? extractMAWB(file2.files[0].name) : "";
  if(!a) return b || "EML";
  if(!b) return a;
  let isAWB = s => /\\d{3}[- ]?\\d{7,8}|\\d{10,11}/.test(s);
  if(isAWB(b) && !isAWB(a)) return b;
  if(["upload","eml","file","manifest"].includes(a.toLowerCase()) && !["upload","eml"].includes(b.toLowerCase())) return b;
  return a;
}

function fmt(n){
  if(n==null || isNaN(n)) return "";
  // keep 2 decimals if needed but strip trailing zeros
  let s = Number(n).toString();
  if(s.includes('.')){
    s = Number(n).toFixed(2).replace(/\\.00$/,'');
  }
  return s;
}

function renderRows(rows){
  tbody.innerHTML = "";
  const maxPreview = 500;
  const preview = rows.length > maxPreview ? rows.slice(0, maxPreview) : rows;
  preview.forEach((r,i)=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="mono">${i+1}</td><td class="mono">${r.Bag}</td><td class="mono">${r.Parcel}</td><td class="mono">${fmt(r["Manifest Weight"])}</td><td class="mono">${fmt(r["Bag Weight"])}</td>`;
    tbody.appendChild(tr);
  });
  if(rows.length > maxPreview){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="5" style="text-align:center; color:#64748b; padding:12px; background:#f8fafc">Showing first ${maxPreview} of ${rows.length} rows — full data in CSV download</td>`;
    tbody.appendChild(tr);
  }
  rowCount.textContent = rows.length + " bags → " + rows.length + " parcels (1 per bag)" + (rows.length > maxPreview ? ` — preview ${maxPreview}` : "");
  outCard.style.display = rows.length ? "block" : "none";
}

function bindDrop(drop, input, nameEl){
  drop.addEventListener('dragover', e=>{ e.preventDefault(); drop.classList.add('dragover'); });
  drop.addEventListener('dragleave', ()=> drop.classList.remove('dragover'));
  drop.addEventListener('drop', e=>{
    e.preventDefault(); drop.classList.remove('dragover');
    if(e.dataTransfer.files.length){
      // for manifest, allow up to 3 files; for bag list only 1
      if(input===file1 && e.dataTransfer.files.length>3){
        msg.innerHTML = '<div class="alert alert-warn">Max 3 manifest files allowed — only first 3 will be used.</div>';
      }
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event('change'));
    }
  });
  input.addEventListener('change', ()=>{
    if(input===file1){
      if(!input.files.length){ nameEl.textContent = ""; }
      else if(input.files.length===1){
        const f = input.files[0];
        const sz = f.size ? ` (${(f.size/1024).toFixed(1)} KB)` : "";
        nameEl.textContent = f.name + sz;
      }
      else {
        let names = Array.from(input.files).slice(0,3).map(f=>f.name).join(', ');
        let totalKB = Array.from(input.files).slice(0,3).reduce((s,f)=>s+f.size,0)/1024;
        nameEl.textContent = input.files.length + " files: " + names + (input.files.length>3 ? " … (only first 3 used)" : "") + " ("+totalKB.toFixed(1)+" KB total)";
        if(input.files.length>3) msg.innerHTML = '<div class="alert alert-warn">Max 3 manifest files allowed — only first 3 will be used.</div>';
      }
      // enforce 3-file limit visually
      if(input.files.length>3){
        try{
          const dt = new DataTransfer();
          Array.from(input.files).slice(0,3).forEach(f=>dt.items.add(f));
          input.files = dt.files;
        }catch(e){}
      }
      clear1.style.display = input.files.length ? 'inline-block' : 'none';
    } else {
      nameEl.textContent = input.files[0] ? input.files[0].name + " (" + (input.files[0].size/1024).toFixed(1) + " KB)" : "";
      clear2.style.display = input.files.length ? 'inline-block' : 'none';
    }
    gen.disabled = !file1.files.length;
    if(!file1.files.length){ clear1.style.display='none'; }
    if(!file2.files.length){ clear2.style.display='none'; }
    // immediate size-limit indicator on every selection
    const all = [...Array.from(file1.files||[]), ...Array.from(file2.files||[])];
    const over = all.filter(f=>f.size > HARD_MB*1024*1024);
    const warn = all.filter(f=>f.size > WARN_MB*1024*1024 && f.size <= HARD_MB*1024*1024);
    if(over.length){
      msg.innerHTML = `<div class="alert alert-err">🚫 <code>${over[0].name}</code> is ${fmtMB(over[0].size)} — exceeds server limit ~${HARD_MB} MB and will cause Bad Gateway timeout. Please remove it (✕ button) and upload smaller files.</div>`;
      showModal('File exceeds server limit',
        `<code>${over[0].name}</code> is <strong>${fmtMB(over[0].size)}</strong> — server allows max ~${HARD_MB} MB per file.<br>Please split it into smaller files or convert Excel → CSV first, then re-upload.`,
        '🚫', 0);
    } else if(warn.length){
      msg.innerHTML = `<div class="alert alert-warn">⚠️ <code>${warn[0].name}</code> is ${fmtMB(warn[0].size)} (over ${WARN_MB} MB) — large uploads may be slow. The app will auto-convert Excel → CSV and auto-split to avoid Bad Gateway timeout.</div>`;
    }
  });
}
bindDrop($('#drop1'), file1, fname1);
bindDrop($('#drop2'), file2, fname2);
clear1.addEventListener('click', ()=>{
  file1.value=''; fname1.textContent=''; clear1.style.display='none';
  gen.disabled = true; msg.innerHTML='<div class="alert alert-warn" style="background:#fffbeb; border-color:#fcd34d">Manifest file(s) removed. Please re-upload.</div>';
  // keep bag file as is
});
clear2.addEventListener('click', ()=>{
  file2.value=''; fname2.textContent=''; clear2.style.display='none';
  msg.innerHTML='<div class="alert alert-warn" style="background:#fffbeb; border-color:#fcd34d">Bag list file removed. Please re-upload if needed for EML.</div>';
});

gen.addEventListener('click', async ()=>{
  if(!file1.files.length){ msg.innerHTML = '<div class="alert alert-err">Please choose 1-3 Parcel Manifest file(s).</div>'; return; }
  if(file1.files.length>3){ msg.innerHTML = '<div class="alert alert-err">Max 3 manifest files allowed.</div>'; return; }
  if(carrier.value==='eml' && !file2.files[0]){
    msg.innerHTML = '<div class="alert alert-warn">EML needs a Bag List file (Bag ID + Carton Weight). Please attach it, or switch to Auto/Skywin/VF.</div>';
  }
  // Hard size gate: block anything over HARD_MB before upload to avoid Bad Gateway
  const gate = checkSizesOrBlock();
  if(gate.blocked){ return; }
  // Large-file strategy: 1) Excel→CSV conversion (fast CSV processing server-side),
  // 2) auto-split for single large files, 3) chunked+gzip fallback.
  // Convert ALL Excel files to CSV first — avoids slow server-side Excel parsing
  // which is the #1 cause of gateway timeouts with multiple files.
  const hasExcel = Array.from(file1.files).some(f=>{
    const n=f.name.toLowerCase(); return n.endsWith('.xlsx')||n.endsWith('.xls')||n.endsWith('.xlsb');
  });
  if(hasExcel && typeof XLSX!=='undefined'){
    msg.innerHTML = '<div class="alert alert-warn">Converting Excel → CSV for faster processing…</div>';
    gen.textContent = "Converting…"; updateProgress(10);
    await convertExcelToCSV(file1);
    if(file2.files[0]){
      const bn = file2.files[0].name.toLowerCase();
      if((bn.endsWith('.xlsx')||bn.endsWith('.xls')||bn.endsWith('.xlsb')) && typeof XLSX!=='undefined'){
        await convertExcelToCSV(file2);
      }
    }
    updateProgress(null);
    msg.innerHTML = `<div class="alert alert-ok">Excel files converted to CSV — now uploading…</div>`;
  }
  // Also auto-split if single file still > 5MB after conversion (e.g. very large CSV)
  const bigSingle = file1.files.length===1 && file1.files[0].size > 5*1024*1024;
  if(bigSingle && typeof XLSX!=='undefined'){
    msg.innerHTML = '<div class="alert alert-warn">Large file detected — auto-splitting for faster processing…</div>';
    gen.textContent = "Splitting…"; updateProgress(10);
    const did = await autoSplitIfLarge(file1);
    updateProgress(null);
    if(did){
      msg.innerHTML = `<div class="alert alert-ok">Auto-split into ${file1.files.length} CSV files — now uploading…</div>`;
    }
  }
  const totalSize = Array.from(file1.files).reduce((s,f)=>s+f.size,0);
  const shouldChunk = file1.files.length===1 && file1.files[0].size > 5*1024*1024 && typeof pako!=='undefined';
  if(shouldChunk){
    msg.innerHTML = `<div class="alert alert-warn">Large file (${fmtMB(totalSize)}) — will use chunked+gzip upload to avoid 504.</div>`;
  }
  gen.disabled = true; gen.textContent = "Generating…";
  if(!msg.innerHTML.includes('Auto-split') && !msg.innerHTML.includes('chunked') && !msg.innerHTML.includes('✅')) msg.innerHTML="";
  dl.disabled = true; meta.textContent=""; updateProgress(5);
  const fd = new FormData();
  // send up to 3 manifest files as `files` (backend also accepts `file` for backward compat)
  Array.from(file1.files).slice(0,3).forEach(f=> fd.append('files', f));
  if(file2.files[0]) fd.append('bag_file', file2.files[0]);
  fd.append('carrier', carrier.value);
  async function readRes(res){
    const ct = res.headers.get('content-type')||'';
    let data;
    if(ct.includes('application/json')){ data = await res.json(); }
    else {
      const txt = await res.text();
      if(isGatewayErrorStatus(res.status) || isGatewayErrorText(txt)){
        handleGatewayFailure('Upload', res.status, txt);
        throw new Error(`Bad Gateway (HTTP ${res.status}): server upload/timeout limit reached. Page will auto-refresh.`);
      }
      try{ data = JSON.parse(txt); } catch(_){ throw new Error(txt.slice(0,600).replace(/<[^>]*>/g,' ').trim() || ('HTTP '+res.status+' '+res.statusText)); }
    }
    if(!res.ok){
      if(isGatewayErrorStatus(res.status) || isGatewayErrorText(JSON.stringify(data))){
        handleGatewayFailure('Upload', res.status, JSON.stringify(data));
        throw new Error(`Bad Gateway (HTTP ${res.status}): ${(data&&data.detail)||'server limit reached'}. Page will auto-refresh.`);
      }
      throw new Error(data.detail || JSON.stringify(data));
    }
    return data;
  }
  try{
    let res, data;
    const useChunked = shouldChunk && file1.files.length === 1;
    if(useChunked){
      const file = file1.files[0];
      const chunkSize = 1 * 1024 * 1024; // 1 MB chunks for slow corporate links (113KB/s → 9s per chunk)
      const totalChunks = Math.ceil(file.size / chunkSize);
      const uploadId = Date.now().toString(36) + Math.random().toString(36).slice(2);
      msg.innerHTML = `<div class="alert alert-warn">Uploading large file via chunked+gzip: ${totalChunks} chunks…</div>`;
      for(let i=0; i<totalChunks; i++){
        const start = i * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const chunkBlob = file.slice(start, end);
        const chunkBuf = await chunkBlob.arrayBuffer();
        const gzipped = pako.gzip(new Uint8Array(chunkBuf));
        const chunkFile = new File([gzipped], `chunk_${i}`, {type: 'application/octet-stream'});
        const cfd = new FormData();
        cfd.append('chunk', chunkFile);
        cfd.append('upload_id', uploadId);
        cfd.append('chunk_index', i);
        cfd.append('total_chunks', totalChunks);
        cfd.append('filename', file.name);
        cfd.append('is_gzipped', 'true');
        let cr;
        try{ cr = await fetch('/api/upload/chunk', { method:'POST', body: cfd }); }
        catch(netErr){ handleGatewayFailure('Chunked upload', 0, String(netErr)); throw new Error('Network/Bad Gateway during chunked upload. Page will auto-refresh.'); }
        if(!cr.ok){
          if(isGatewayErrorStatus(cr.status)){ const t = await cr.text(); handleGatewayFailure('Chunked upload', cr.status, t); throw new Error(`Bad Gateway on chunk ${i+1}/${totalChunks} (HTTP ${cr.status}). Page will auto-refresh.`); }
          const txt = await cr.text();
          let j; try{ j=JSON.parse(txt);}catch(_){ j={detail:txt.slice(0,500)}; }
          if(isGatewayErrorText(txt)||isGatewayErrorText(j.detail)){ handleGatewayFailure('Chunked upload', cr.status, txt); throw new Error(`Bad Gateway on chunk ${i+1}. Page will auto-refresh.`); }
          throw new Error(j.detail || `Chunk ${i} failed`);
        }
        meta.textContent = `Uploading chunk ${i+1}/${totalChunks}…`;
        updateProgress(Math.round(((i+1)/totalChunks)*90));
      }
      // Complete
      const cfd2 = new FormData();
      cfd2.append('upload_id', uploadId);
      cfd2.append('filename', file.name);
      cfd2.append('carrier', carrier.value);
      cfd2.append('total_chunks', totalChunks);
      let cres;
      try{ cres = await fetch('/api/upload/complete', { method:'POST', body: cfd2 }); }
      catch(netErr){ handleGatewayFailure('Assembling chunks', 0, String(netErr)); throw new Error('Network/Bad Gateway while assembling. Page will auto-refresh.'); }
      data = await readRes(cres);
      updateProgress(100);
    } else {
      updateProgress(30);
      let fetchRes;
      try{ fetchRes = await fetch('/api/generate', { method:'POST', body: fd }); }
      catch(netErr){ updateProgress(null); handleGatewayFailure('Upload', 0, String(netErr)); throw new Error('Network error / Bad Gateway: server unreachable or timed out. Page will auto-refresh.'); }
      updateProgress(80);
      data = await readRes(fetchRes);
      updateProgress(100);
    }
    lastRows = data.rows || [];
    lastFilename = data.filename || safeCsvFilename(data.mawb || currentMAWB());
    if(data.mawb) lastFilename = safeCsvFilename(data.mawb);
    dl.textContent = lastRows.length ? `Download ${lastFilename}` : 'Download CSV';
    renderRows(lastRows);
    meta.textContent = `Parsed ${data.parcels_raw} parcel rows → ${data.bags_unique} unique bags. ` + (data.excluded ? `(${data.excluded} junk-ID row(s) auto-excluded) ` : '') + (data.carrier ? '('+data.carrier+') ' : '');

    if(data.warnings && data.warnings.length){
      msg.innerHTML = '<div class="alert alert-warn">' + data.warnings.join('<br>') + '</div>';
    } else {
      msg.innerHTML = '<div class="alert alert-ok">Generated ' + lastRows.length + ' rows. Manifest Weight = Bag Weight per bag. Random 1-per-bag selection applied. Parcels starting with 6 prefixed with JT. CSV will download as <code>'+lastFilename+'</code>.</div>';
    }
    dl.disabled = lastRows.length===0;
    updateProgress(null);
  }catch(e){
    updateProgress(null);
    const msgTxt = (e.message||String(e)).slice(0,800);
    const clean = msgTxt.replace(/<[^>]*>/g,' ').replace(/\\s+/g,' ').trim();
    if(/bad gateway|gateway timeout|network error|failed to fetch|load failed|504|502/i.test(clean) && !modalBg().classList.contains('show')){
      handleGatewayFailure('Request', 0, clean);
    }
    msg.innerHTML = '<div class="alert alert-err">' + clean + '</div>';
  }finally{
    gen.disabled = false; gen.textContent = "Generate";
  }
});

dl.addEventListener('click', ()=>{
  if(!lastRows.length) return;
  let csv = "Bag,Parcel,Manifest Weight,Bag Weight\\n";
  lastRows.forEach(r=>{
    const esc = v => {
      let s = String(v);
      if(s.includes(',') || s.includes('"') || s.includes('\\n')) s = '"' + s.replace(/"/g,'""') + '"';
      return s;
    };
    csv += [esc(r.Bag), esc(r.Parcel), esc(r["Manifest Weight"]), esc(r["Bag Weight"])].join(',') + "\\n";
  });
  const blob = new Blob([csv], {type:'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = lastFilename || safeCsvFilename(currentMAWB()); a.click();
  URL.revokeObjectURL(url);
});

// allow pressing Enter
document.addEventListener('keydown', e=>{ if(e.key==='Enter' && file1.files.length) gen.click(); });
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def homepage():
    return PAGE.replace("__APP_NAME__", APP_NAME)
