"""psf_read — read a Spectre PSF result (transient or DC), ASCII **or** binary.

One entry point::

    swp_name, swp_values, signals = read_psf("…/tranAn.tran.tran")

``signals`` maps signal name -> float ndarray over the sweep axis, so
``signals["M1:gm"]`` is the transconductance Spectre itself computed at every
point of a run that was netlisted with ``save M1:oppoint``.

PSF-ASCII is delegated to ``psf_utils`` when it is installed. PSF **binary**
(what ADE writes by default, and what ``psf_utils`` refuses) is parsed here:
big-endian chunked sections located from the trailing ``Clarissa`` index —

  header   properties (``PSF sweep points``, ``PSF window size``, …)
  types    id -> (name, storage kind)
  sweep    the sweep variable (``time`` / a DC parameter)
  traces   ordered signal names
  values   the numbers, in one of two layouts:
             * non-windowed — ``(0x10, id, value)`` triplets, point by point
             * windowed     — fixed 4096-byte windows, 8 header bytes then
               doubles: one window for the sweep variable followed by one per
               trace, that whole round repeating until every point is written.

PSF-XL (``psfxl``) is a different, compressed container and is *not* supported;
re-run with ``-format psfascii`` or ``-format psfbin`` for those.
"""
import contextlib
import mmap
import os
import re
import struct
import time

import numpy as np

# chunk codes shared by every section
_PROP_STR, _PROP_INT, _PROP_DBL = 0x21, 0x22, 0x23
_DECL, _GROUP, _SECTION, _SUBSEC, _WINDOW = 0x10, 0x11, 0x15, 0x16, 0x14

# PSF type codes -> (struct format, byte size); None = not a plain number
_TYPES = {1: ("b", 1), 2: (None, 0), 5: (">i", 4), 11: (">d", 8), 12: (">d", 16)}

_ANALYSIS_SUFFIX = (".tran.tran", ".dc", ".ac", ".tran", ".dcOp", ".info")


# ---------------------------------------------------------------- binary PSF
@contextlib.contextmanager
def mapped(path):
    """The file as a memory map.

    The parsers index the whole file absolutely, but a summary only touches the
    header, the trailing index and the metadata sections — mapping instead of
    reading means a multi-gigabyte waveform costs a few pages, not a full copy.
    That is the difference between the tool answering at once and appearing to
    hang while it slurps every analysis in the run."""
    with open(path, "rb") as f:
        if os.path.getsize(path) == 0:
            yield b""
            return
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            yield mm
        finally:
            mm.close()


class _Reader:
    def __init__(self, buf):
        self.b, self.p = buf, 0

    def i32(self):
        v = struct.unpack_from(">i", self.b, self.p)[0]
        self.p += 4
        return v

    def peek(self):
        return struct.unpack_from(">i", self.b, self.p)[0]

    def dbl(self):
        v = struct.unpack_from(">d", self.b, self.p)[0]
        self.p += 8
        return v

    def string(self):
        n = self.i32()
        s = self.b[self.p:self.p + n].decode("latin-1")
        self.p += (n + 3) // 4 * 4          # padded to a 4-byte boundary
        return s

    def props(self, stop):
        """Read the (name, value) property chunks that trail a declaration."""
        out = {}
        while self.p < stop:
            code = self.peek()
            if code == _PROP_STR:
                self.p += 4; k = self.string(); out[k] = self.string()
            elif code == _PROP_INT:
                self.p += 4; k = self.string(); out[k] = self.i32()
            elif code == _PROP_DBL:
                self.p += 4; k = self.string(); out[k] = self.dbl()
            else:
                break
        return out


def _sections(buf):
    """Locate the section offsets from the trailing index (…, 'Clarissa', off)."""
    if buf[-12:-4] != b"Clarissa":
        raise ValueError("no 'Clarissa' trailer — not a plain binary PSF (a PSF-XL "
                         "index, a partial/truncated file, or another PSF variant)")
    toc = struct.unpack(">I", buf[-4:])[0]
    r = _Reader(buf); r.p = toc
    if r.peek() == 0x0F:                                # optional index marker
        r.i32()
    r.i32()                                             # reserved (0)
    n = r.i32()                                         # number of sections
    out = {}
    for _ in range(n):
        if r.p + 8 > len(buf) - 12:
            break
        sid = r.i32(); out[sid] = r.i32()
    return out


def _read_header(buf):
    r = _Reader(buf); r.p = 4                            # skip the leading size word
    if r.i32() != _SECTION:
        raise ValueError("unexpected PSF header section")
    end = r.i32()
    return r.props(end)


def _read_types(buf, off):
    """id -> (name, struct fmt, size). Types carry the storage kind of a trace."""
    r = _Reader(buf); r.p = off
    r.i32(); end = r.i32()
    if r.peek() == _SUBSEC:
        r.i32(); end = r.i32()
    types = {}
    while r.p < end and r.peek() == _DECL:
        r.i32()
        tid = r.i32(); name = r.string()
        r.i32()                                          # array flag
        code = r.i32()
        fmt, size = _TYPES.get(code, (None, 0))
        types[tid] = (name, fmt, size)
        r.props(end)
    return types


def _read_decls(buf, off):
    """The sweep and trace sections share one layout: id, name, type id, props."""
    r = _Reader(buf); r.p = off
    r.i32(); end = r.i32()
    if r.peek() == _SUBSEC:
        r.i32(); end = r.i32()
    if r.peek() == _GROUP:                               # tran: traces wrapped in a group
        r.i32(); r.i32(); r.string(); r.i32()
    out = []
    while r.p < end and r.peek() == _DECL:
        r.i32()
        tid = r.i32(); name = r.string(); type_id = r.i32()
        out.append((tid, name, type_id))
        r.props(end)
    return out


def _values_windowed(buf, start, end, npoints, ntraces, winsize):
    """Windowed layout: [sweep window][trace 0]…[trace N-1], repeated.

    Each window is ``winsize`` bytes: 8 bytes of header/fill then doubles. The
    sweep window's header carries (slots, points-in-this-window)."""
    slots = winsize // 8 - 1
    swp = np.empty(npoints); data = np.empty((ntraces, npoints))
    pos, done = start, 0
    while done < npoints and pos + winsize <= end:
        n = struct.unpack_from(">H", buf, pos + 6)[0]    # points in this window
        n = min(n if 0 < n <= slots else slots, npoints - done)
        swp[done:done + n] = np.frombuffer(buf, ">f8", n, pos + 8)
        for j in range(ntraces):
            o = pos + (j + 1) * winsize + 8
            if o + 8 * n > end:
                raise ValueError("truncated PSF window buffer")
            data[j, done:done + n] = np.frombuffer(buf, ">f8", n, o)
        pos += (ntraces + 1) * winsize
        done += n
    if done < npoints:
        swp, data = swp[:done], data[:, :done]
    return swp, data


def _values_flat(buf, start, end, ids, types, npoints):
    """Non-windowed layout: (0x10, id, value) triplets, one round per point."""
    r = _Reader(buf); r.p = start
    order = {tid: k for k, tid in enumerate(ids)}
    fmt = {tid: types.get(tt, (None, ">d", 8))[1:] for tid, tt in ids.items()} \
        if isinstance(ids, dict) else None
    vals = [[] for _ in order]
    while r.p + 8 <= end and r.peek() == _DECL:
        r.i32()
        tid = r.i32()
        if tid not in order:
            break
        f, size = (fmt or {}).get(tid, (">d", 8))
        if f is None or size == 0:
            raise ValueError("non-numeric PSF trace type")
        v = struct.unpack_from(f, buf, r.p)[0]
        r.p += size
        vals[order[tid]].append(v)
    n = min(len(v) for v in vals) if vals else 0
    if npoints:
        n = min(n, npoints)
    return np.array([v[:n] for v in vals], dtype=float)


# ------------------------------------------------- struct PSF ("*.info" files)
_STRUCT, _STRUCT_END = 16, 0x12

# a struct member is stored by its own type code
_MEMBER = {11: ("dbl", 8), 5: ("i32", 4), 1: ("i32", 4), 2: ("str", 0), 4: ("str", 0)}


def _read_typedef(r, end):
    """One type declaration; a struct (code 16) carries a nested member list
    terminated by 0x12, then its own properties."""
    r.i32()                                              # DECL
    t = {"id": r.i32(), "name": r.string()}
    r.i32()                                              # array flag
    t["code"] = r.i32()
    t["members"] = []
    if t["code"] == _STRUCT:
        while r.p < end:
            if r.peek() == _STRUCT_END:
                r.i32(); break
            if r.peek() != _DECL:
                break
            t["members"].append(_read_typedef(r, end))
    r.props(end)
    return t


def read_typedefs(buf, off):
    """{type id: typedef} for a file whose TYPE section defines structs."""
    r = _Reader(buf); r.p = off
    r.i32(); end = r.i32()
    if r.peek() == _SUBSEC:
        r.i32(); end = r.i32()
    out = {}
    while r.p < end and r.peek() == _DECL:
        t = _read_typedef(r, end)
        out[t["id"]] = t
    return out


def _read_struct_value(r, t, types):
    if t["code"] == _STRUCT:
        return {m["name"]: _read_struct_value(r, m, types) for m in t["members"]}
    kind = _MEMBER.get(t["code"])
    if kind is None:
        if t["code"] in types:                           # a named type reference
            return _read_struct_value(r, types[t["code"]], types)
        raise ValueError(f"unsupported PSF member type {t['code']} ({t['name']})")
    if kind[0] == "dbl":
        return r.dbl()
    if kind[0] == "i32":
        return r.i32()
    return r.string()


def read_psf_info(path):
    """A struct-valued PSF (``finalTimeOP.info``, ``element.info``, …).

    These carry no sweep: the value section is one entry per circuit element,
    each a struct of that element's quantities — the whole operating point of a
    run (``finalTimeOP``) or every instance's parameters (``element``). ADE
    writes them beside the waveform, so a run whose waveform is PSF-XL still has
    a readable operating point here.

    Returns ``(header, {instance: {param: value}})`` with the escaped instance
    names (``M6\\<6\\>``) left as Spectre wrote them. Memoized per file, so
    element.info is parsed once however many devices ask for it."""
    key = _fingerprint(path)
    if key in _INFO_CACHE:
        return _INFO_CACHE[key]
    with mapped(path) as buf:
        return _read_info(path, buf)


def _read_info(path, buf, _key=None):
    hdr = _read_header(buf)
    sec = _sections(buf)
    if 1 not in sec or 4 not in sec:
        raise ValueError(f"{os.path.basename(path)}: not a struct PSF (no type/value section)")
    types = read_typedefs(buf, sec[1])
    r = _Reader(buf); r.p = sec[4]
    r.i32(); end = r.i32()
    if r.peek() == _SUBSEC:
        r.i32(); end = r.i32()
    out = {}
    while r.p < end and r.peek() == _DECL:
        r.i32()
        r.i32()                                          # element id
        name = r.string(); tid = r.i32()
        t = types.get(tid)
        if t is None:
            break
        out[name] = _read_struct_value(r, t, types)
        r.props(end)
    try:
        _INFO_CACHE[_fingerprint(path)] = (hdr, out)
    except OSError:
        pass
    return hdr, out

def op_point_file(path):
    """The operating-point info file beside a result, if the run wrote one."""
    d = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    for name in ("finalTimeOP.info", "dcOpInfo.info", "opBegin.info"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def element_file(path):
    """``element.info`` beside a result — every instance's parameters (w, l, m…)."""
    d = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    p = os.path.join(d, "element.info")
    return p if os.path.exists(p) else None


def _no_sweep_msg(path, hdr, sec, sweeps, traces):
    """Say *why* a PSF has nothing to plot — the common case is an operating
    point (an unswept ``dc``, or a dcOp/info file), which has no sweep axis."""
    kind = hdr.get("analysis type", "?")
    name = hdr.get("analysis name", "?")
    hint = ("this is an operating point, not a swept result — netlist a `dc` with a "
            "swept parameter/source, or a `tran`, and point --result/--analysis at that"
            if not sweeps else
            "the file declares a sweep but no traces — was `save <inst>:oppoint` netlisted?")
    return (f"{os.path.basename(path)}: no swept traces to read. "
            f"analysis '{name}' (type {kind}), "
            f"sweeps={len(sweeps)}, traces={len(traces)}, "
            f"points={hdr.get('PSF sweep points', 0)}, sections={sorted(sec)}. {hint}")


def structure(path, cache=True):
    """What the reader sees in a PSF file, without decoding its values.

    Cheap enough to run over every candidate file, so the CLI can say which
    analyses a run holds and which of them are actually swept. Memoized: with a
    thousand transistors the same handful of files would otherwise be re-parsed
    a thousand times, which is where a slow run's time actually went."""
    if cache:
        try:
            key = _fingerprint(path)
        except OSError:
            key = None
        if key is not None and key in _STRUCT_CACHE:
            return _STRUCT_CACHE[key]
    out = {"path": path, "file": os.path.basename(path),
           "size": os.path.getsize(path), "format": "binary", "error": None,
           "analysis": None, "type": None, "sweeps": 0, "traces": 0,
           "points": 0, "windowed": 0, "sweep_name": None, "sample": []}
    try:
        if not is_binary_psf(path):
            # PSF-ASCII has no "PSF sweeps"/"PSF traces" properties — it carries
            # plain section keywords, so count the declarations in each section
            out["format"] = "ascii"
            section, names, in_prop = None, {"SWEEP": [], "TRACE": []}, False
            head = []
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if in_prop:                    # inside PROP( … ): "units" "s" etc
                        in_prop = s != ")"         # looks like a declaration, isn't
                        continue
                    if s in ("HEADER", "TYPE", "SWEEP", "TRACE", "VALUE", "END"):
                        section = s
                        if s in ("VALUE", "END"):
                            break
                        continue
                    opens = s.endswith("PROP(")
                    if section == "HEADER":
                        head.append(s)
                    elif section in names:
                        m = re.match(r'"([^"]+)"\s+"[^"]*"', s)   # "name" "type" …
                        if m:
                            names[section].append(m.group(1))
                    in_prop = opens
            head = "\n".join(head)
            out["analysis"] = _ascii_prop(head, "analysis name")
            out["type"] = _ascii_prop(head, "analysis type")
            out["sweeps"] = len(names["SWEEP"])
            out["traces"] = len(names["TRACE"])
            out["sweep_name"] = names["SWEEP"][0] if names["SWEEP"] else None
            out["sample"] = names["TRACE"][:6]
            if cache and key is not None:
                _STRUCT_CACHE[key] = out
            return out
        xl = psfxl_sidecar(path)
        if xl:
            out["format"] = "psfxl"
            hdr = _read_header(open(path, "rb").read())
            out.update(analysis=hdr.get("analysis name"), type=hdr.get("analysis type"),
                       error=f"PSF-XL (values in {os.path.basename(xl)}) — re-run with "
                             f"-format psfbin or -format psfascii")
            if cache and key is not None:
                _STRUCT_CACHE[key] = out
            return out
        with mapped(path) as buf:
            return _structure_binary(path, buf, out)
    except Exception as exc:                       # a summary must never itself fail
        out["error"] = f"{type(exc).__name__}: {exc}"
    if cache and key is not None:
        _STRUCT_CACHE[key] = out
    return out


def _structure_binary(path, buf, out):
    try:
        hdr = _read_header(buf); sec = _sections(buf)
        if hdr.get("analysis type") == "info" or path.endswith(".info"):
            try:
                _h, elems = read_psf_info(path)
                fets = [k for k, v in elems.items()
                        if isinstance(v, dict) and "gm" in v and "ids" in v]
                out.update(format="info", analysis=hdr.get("analysis name"),
                           type="info", traces=len(elems), sweeps=0, points=1,
                           sample=fets[:6],
                           error=None if fets else "no device operating points")
                out["fets"] = len(fets)
            except Exception as exc:
                out.update(type="info", error=f"{type(exc).__name__}: {exc}")
            return out
        sweeps = _read_decls(buf, sec[2]) if 2 in sec else []
        traces = _read_decls(buf, sec[3]) if 3 in sec else []
        out.update(analysis=hdr.get("analysis name"), type=hdr.get("analysis type"),
                   points=int(hdr.get("PSF sweep points", 0)),
                   windowed=int(hdr.get("PSF window size", 0) or 0),
                   sweeps=len(sweeps), traces=len(traces),
                   sweep_name=(sweeps[0][1] if sweeps else None),
                   sample=[t[1] for t in traces[:6]], sections=sorted(sec))
    except Exception as exc:                       # a summary must never itself fail
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _ascii_prop(text, key):
    m = re.search(r'"%s"\s+"?([^"\n]+)"?' % re.escape(key), text)
    return m.group(1).strip() if m else None


def is_swept(path):
    """True when the file carries a sweep axis and traces — i.e. something to read."""
    st = structure(path)
    return bool(st["sweeps"]) and bool(st["traces"]) and not st["error"]


def psfxl_sidecar(path):
    """PSF-XL writes a small PSF *index* here and the values in ``<path>.psfxl``.

    The index parses as an ordinary binary PSF — same magic, same header — but
    reports 0 sweep points and carries no value section, so the give-away is the
    sidecar (spectre's log says "Opening the PSFXL file" for the same run)."""
    xl = path + ".psfxl"
    return xl if os.path.exists(xl) else None


def read_psf_binary(path):
    """Parse a binary PSF sweep result -> (sweep name, sweep values, {name: array})."""
    xl = psfxl_sidecar(path)
    if xl:
        raise ValueError(
            f"{os.path.basename(path)}: this is PSF-XL — the values live in "
            f"{os.path.basename(xl)}, a compressed Cadence container this reader does "
            f"not support. Re-run the analysis with `-format psfbin` or "
            f"`-format psfascii` (both are read here).")
    with mapped(path) as buf:
        return _read_binary(path, buf)


def _read_binary(path, buf):
    hdr = _read_header(buf)
    sec = _sections(buf)
    if 4 not in sec:
        raise ValueError("PSF file has no value section")
    types = _read_types(buf, sec[1]) if 1 in sec else {}
    sweeps = _read_decls(buf, sec[2]) if 2 in sec else []
    traces = _read_decls(buf, sec[3]) if 3 in sec else []
    if not sweeps or not traces:
        raise ValueError(_no_sweep_msg(path, hdr, sec, sweeps, traces))
    npoints = int(hdr.get("PSF sweep points", 0))

    r = _Reader(buf); r.p = sec[4]
    r.i32(); vend = r.i32()
    win = int(hdr.get("PSF window size", 0) or 0)
    if win:
        # windows are aligned to the window size inside the value section
        first = r.p
        if r.peek() == _WINDOW:
            r.i32(); first = r.p + r.i32()
        first = (first + win - 1) // win * win
        swp, data = _values_windowed(buf, first, min(vend, len(buf)), npoints,
                                     len(traces), win)
    else:
        idmap = {tid: tt for tid, _n, tt in ([sweeps[0]] + traces)}
        arr = _values_flat(buf, r.p, min(vend, len(buf)), idmap, types, npoints)
        swp, data = arr[0], arr[1:]
    sig = {name: data[j] for j, (_tid, name, _tt) in enumerate(traces)
           if j < len(data)}
    return sweeps[0][1], np.asarray(swp, dtype=float), sig


# ---------------------------------------------------------------- ASCII PSF
def read_psf_ascii(path):
    from psf_utils import PSF
    psf = PSF(path)
    swp = psf.sweeps[0]
    x = np.real(np.asarray(swp.abscissa)).astype(float)
    sig = {}
    for s in psf.all_signals():
        v = np.real(np.asarray(s.ordinate)).astype(float)
        if v.ndim == 1 and len(v) == len(x):
            sig[s.name] = v
    return swp.name, x, sig


def is_binary_psf(path):
    with open(path, "rb") as f:
        head = f.read(4)
    return head[:1] == b"\x00"


def _fingerprint(path):
    """(path, mtime, size) — enough to key a cache without re-reading anything."""
    st = os.stat(path)
    return (os.path.abspath(path), st.st_mtime, st.st_size)


_CACHE = {}          # one entry: analyzing every device in a run re-reads one file
_STRUCT_CACHE = {}   # per-file summaries; a 1000-device run asks for the same ones
_INFO_CACHE = {}     # parsed *.info structs (element.info is read once, not per FET)
_FIND_CACHE = {}     # which analysis file a given --result resolves to


def read_psf(path, cache=True, progress=None):
    """Read a PSF result file (ASCII or binary) -> (sweep name, x, {name: y})."""
    st = os.stat(path)
    if progress:
        progress(f"reading {os.path.basename(path)} "
                 f"({st.st_size / 1e6:.1f} MB, "
                 f"{'binary' if is_binary_psf(path) else 'ascii'}) …")
    t0 = time.time()
    key = (os.path.abspath(path), st.st_mtime, st.st_size)
    if cache and key in _CACHE:
        if progress:
            progress("(already read — using the cached parse)")
        return _CACHE[key]
    if is_binary_psf(path):
        out = read_psf_binary(path)
    else:
        try:
            out = read_psf_ascii(path)
        except ImportError:
            raise RuntimeError("psf_utils is required to read PSF-ASCII results "
                               "(pip install psf_utils)")
    if cache:
        _CACHE.clear(); _CACHE[key] = out
    if progress:
        progress(f"read {len(out[2])} signals x {len(out[1])} points "
                 f"in {time.time() - t0:.1f} s")
    return out


# ------------------------------------------------------- locating the result
def _is_log(path):
    return os.path.isfile(path) and path.endswith((".out", ".log", ".txt"))


def read_log(path):
    """Mine a Spectre log for everything needed to find the run's data.

    A ``spectre.out`` states it all outright::

        Current working directory: /work/sim
        ... -64 mirror.scs -format psfbin -raw ./mir_raw ...
        Reading file:  /work/sim/mirror.scs
        Opening the PSF file ./mir_raw/tranAn.tran.tran ...

    -> {cwd, netlist, psf_files[], raw_dirs[]}, all absolute."""
    txt = open(path, encoding="utf-8", errors="ignore").read(2000000)
    base = os.path.dirname(os.path.abspath(path))
    m = re.search(r"^Current working directory:\s*(\S+)", txt, re.M)
    cwd = m.group(1) if m and os.path.isdir(m.group(1)) else base

    def absolute(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(cwd, p))

    psf = [absolute(p) for p in re.findall(r"Opening the PSF file\s+(\S+)", txt)]
    raws = [absolute(d) for d in re.findall(r"-raw\s+(\S+)", txt)]
    raws += [os.path.dirname(p) for p in psf]
    netlist = None
    for f in re.findall(r"^Reading file:\s+(\S+)", txt, re.M):
        f = absolute(f)
        if f.endswith((".scs", ".sp", ".cir", ".net")) and os.path.exists(f):
            netlist = f                                   # the input deck is read first
            break
    return {"cwd": cwd, "netlist": netlist,
            "psf_files": [p for p in dict.fromkeys(psf) if os.path.exists(p)],
            "raw_dirs": [d for d in dict.fromkeys(raws) if os.path.isdir(d)]}


def result_files(path):
    """Every PSF tran/dc analysis file a user-supplied path leads to.

    Accepts the file itself, a raw/psf directory, a run directory (``*.raw``,
    ``psf/``) or a ``spectre.out`` log — the same "point me at your run"
    convenience the characterizer has."""
    path = os.path.expanduser(path)
    if os.path.isfile(path) and not _is_log(path) and not path.endswith(".scs"):
        return [path]
    cands, direct = [], []
    if _is_log(path):
        log = read_log(path)
        direct = list(log["psf_files"])
        cands = log["raw_dirs"] + [log["cwd"], os.path.join(log["cwd"], "psf")]
    elif os.path.isdir(path):
        cands = [path, os.path.join(path, "psf"), os.path.join(path, "raw")]
        cands += sorted(os.path.join(path, d) for d in os.listdir(path)
                        if os.path.isdir(os.path.join(path, d))
                        and (d.endswith(".raw") or d in ("psf", "raw")))
    seen, files = set(), list(direct)
    for d in cands:
        if not os.path.isdir(d) or d in seen:
            continue
        seen.add(d)
        for fn in sorted(os.listdir(d)):
            if fn.endswith(_ANALYSIS_SUFFIX) and not fn.endswith((".info", ".dcOp")):
                files.append(os.path.join(d, fn))
    out = [f for f in dict.fromkeys(files) if os.path.isfile(f)]
    if not out:
        raise FileNotFoundError(f"no PSF tran/dc result found under {path}")
    return out


def find_result_file(path, analysis=None, debug=False, progress=None):
    """One analysis file out of ``result_files`` — the transient, else the DC.

    Files that carry no sweep axis (an unswept ``dc``, i.e. a plain operating
    point, or a dcOp/info dump) are skipped rather than fatal: a run commonly
    holds one of those beside the analysis you actually want."""
    ckey = (os.path.abspath(os.path.expanduser(path)), analysis)
    if ckey in _FIND_CACHE:
        return _FIND_CACHE[ckey]
    if progress:
        progress(f"locating results under {path} …")
    files = result_files(path)
    if progress:
        progress(f"found {len(files)} analysis file(s)")
    if analysis:
        hit = [f for f in files if analysis in os.path.basename(f)]
        if not hit:
            raise FileNotFoundError(
                f"no analysis matching '{analysis}' — found: "
                + ", ".join(os.path.basename(f) for f in files))
        files = hit
    ordered = [f for f in files if ".tran" in f] + [f for f in files if ".tran" not in f]
    rejected = []
    for f in ordered:
        if progress:
            progress(f"inspecting {os.path.basename(f)} …")
        st = structure(f)
        if debug:
            print(f"  [psf] {st['file']:<28} {st['format']:<6} "
                  f"analysis={st['analysis'] or '?'} type={st['type'] or '?'} "
                  f"sweeps={st['sweeps']} traces={st['traces']} points={st['points']}"
                  + (f" windowed={st['windowed']}" if st.get("windowed") else "")
                  + (f" ERROR {st['error']}" if st["error"] else ""), flush=True)
        if st["sweeps"] and st["traces"] and not st["error"]:
            if progress:
                progress(f"using {os.path.basename(f)} "
                         f"({st['analysis']}, {st['points']} points, "
                         f"{st['traces']} traces)")
            _FIND_CACHE[ckey] = f
            return f
        rejected.append((st, ("unreadable: " + st["error"]) if st["error"] else
                         ("no sweep axis — operating point, not a swept analysis"
                          if not st["sweeps"] else "no traces saved")))
    op = op_point_file(ordered[0] if ordered else path)
    if op:                       # nothing swept, but the run's operating point is here
        if debug:
            print(f"  [psf] no swept analysis readable — falling back to "
                  f"{os.path.basename(op)} (one bias per device)", flush=True)
        _FIND_CACHE[ckey] = op
        return op
    detail = "; ".join(f"{st['file']} (analysis '{st['analysis'] or '?'}', "
                       f"{st['sweeps']} sweeps, {st['traces']} traces): {why}"
                       for st, why in rejected)
    raise FileNotFoundError(
        f"no swept tran/dc result under {path} — checked {len(ordered)} file(s): {detail}")
