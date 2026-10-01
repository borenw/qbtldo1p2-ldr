#!/usr/bin/env python3
"""ldo_report.py - HTML report for the LDO load-regulation test, from an ADE XL run.

    python3 ldo_report.py <adexl>/results/data/Interactive.N.rdb [-o report.html]

Everything comes from what ADE XL already saved, so no Virtuoso, licence or PDK
is needed:

  Interactive.N.rdb   plain SQLite: corner per point, its design variables,
                      every output value, spec limits and ADE's own pass/fail
  <rawDir>/<point>/<test>/psf/dc.dc
                      binary PSF: the full VOUT vs ILOAD sweep per point

The rdb's psfTable names the raw directory, and point folder N is rdb point N.
The page layout mirrors section 4 of https://borenw.github.io/ldo-loadreg-setup/.
"""
import argparse
import datetime
import html
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import psf_read  # noqa: E402  (binary/ASCII PSF reader from gm-estimate-from-spectre)


def q(db, sql, *a):
    return db.execute(sql, a).fetchall()


def num(v):
    """rdb values are stored as REAL or as strings like '10u'; '' means unset."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([-+0-9.eE]+)\s*([fpnumkKMG]?)\s*", str(v))
    if not m:
        return None
    scale = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1,
             "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]
    return float(m.group(1)) * scale


def raw_dir(db, rdb):
    """Where the PSF lives. psfTable.rawDir is absolute; fall back to resultsDir."""
    for raw, res in q(db, "select rawDir, resultsDir from psfTable"):
        for d in (raw, res):
            if d and os.path.isdir(d):
                return d
    # $AXL_PROJECT_DIR is the simulation project dir; try the sibling of the rdb
    guess = os.path.splitext(rdb)[0]
    if os.path.isdir(guess):
        return guess
    sys.exit("ldo_report: cannot find the run's PSF directory (psfTable rawDir/resultsDir)")


def wave_signal(db, test_id, override):
    """Net to plot: --signal, else the VS("/NET") of the first waveform output."""
    if override:
        return override
    for name, expr in q(db, "select name, expression from result where testID=? "
                            "order by resultOrder", test_id):
        m = re.fullmatch(r'\s*V[STF]?\("/?([^"]+)"\)\s*', expr or "")
        if m:
            return m.group(1)
    sys.exit("ldo_report: no plain VS(\"/NET\") output to plot; pass --signal NET")


def collect(rdb, test_name=None, signal=None, result_file="dc.dc"):
    db = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(rdb), uri=True)
    tests = q(db, "select testID, name from test")
    if not tests:
        sys.exit("ldo_report: no tests in %s" % rdb)
    if test_name:
        tests = [t for t in tests if t[1] == test_name] or sys.exit(
            "ldo_report: test %s not in %s" % (test_name, rdb))
    test_id, test = tests[0]

    info = dict(q(db, "select * from maestroViewInfo"))
    prop = dict(q(db, "select * from databaseProperty"))
    run = q(db, "select min(startTime), max(stopTime) from testStatus")[0]
    rawd = raw_dir(db, rdb)
    net = wave_signal(db, test_id, signal)

    # outputs and specs, in ADE's order
    outputs = []
    for rid, name, expr in q(db, "select resultID, name, expression from result "
                                 "where testID=? order by resultOrder", test_id):
        sp = q(db, "select specID, type, expression1, expression2 from spec where resultID=?", rid)
        outputs.append({"id": rid, "name": name, "expr": expr,
                        "spec": ({"id": sp[0][0], "type": sp[0][1], "lim": num(sp[0][2]),
                                  "lim2": num(sp[0][3]), "text": sp[0][2]} if sp else None)})

    params = {pid: n for pid, n in q(db, "select parameterID, name from parameter")}
    points = []
    for pid, corner in q(db, "select p.pointID, c.name from point p "
                             "join corner c on c.cornerID = p.cornerID order by p.pointID"):
        pv = {params.get(k, str(k)): v for k, v in
              q(db, "select parameterID, value from parameterValue where pointID=?", pid)}
        vals = {}
        for o in outputs:
            r = q(db, "select value from resultValue where pointID=? and resultID=?", pid, o["id"])
            vals[o["name"]] = num(r[0][0]) if r and r[0][0] != "wave" else None
        status = {}
        for o in outputs:
            if o["spec"]:
                r = q(db, "select specStatus from specValue where pointID=? and specID=?",
                      pid, o["spec"]["id"])
                status[o["name"]] = (r[0][0] == 0) if r and r[0][0] is not None else None
        f = os.path.join(rawd, str(pid), test, "psf", result_file)
        x = y = None
        if os.path.isfile(f):
            _, xs, sig = psf_read.read_psf(f, cache=False)
            if net in sig:
                x, y = [float(v) for v in xs], [float(v) for v in sig[net]]
        proc = corner.split("_")[0] if corner else "Nominal"
        points.append({"pid": pid, "corner": corner or "Nominal", "proc": proc,
                       "vin": num(pv.get("vin")), "T": num(pv.get("temperature")),
                       "vnom": num(pv.get("VOUT_NOM")), "vals": vals, "status": status,
                       "x": x, "y": y, "psf": f if x else None})

    missing = [p["pid"] for p in points if p["x"] is None]
    vnom = next((p["vnom"] for p in points if p["vnom"]), None)
    when = prop.get("timeCreated", "")
    if run[0]:
        when = datetime.datetime.fromtimestamp(float(run[0])).strftime("%Y-%m-%d %H:%M")
    return {
        "meta": {"lib": info.get("lib", ""), "cell": info.get("cell", ""),
                 "view": info.get("view", ""), "history": info.get("history", ""),
                 "test": test, "net": net, "rdb": os.path.abspath(rdb), "raw": rawd,
                 "when": when, "made": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "missing": missing},
        "vnom": vnom, "outputs": [{k: o[k] for k in ("name", "expr", "spec")} for o in outputs],
        "points": points,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rdb", help="ADE XL results .rdb (…/adexl/results/data/Interactive.N.rdb)")
    ap.add_argument("-o", "--out", help="output HTML (default: ldo_loadreg_<history>.html here)")
    ap.add_argument("--test", help="test name (default: the first test in the run)")
    ap.add_argument("--signal", help="net to plot (default: from the first VS(\"/NET\") output)")
    a = ap.parse_args()

    data = collect(a.rdb, a.test, a.signal)
    m = data["meta"]
    out = a.out or "ldo_loadreg_%s.html" % (m["history"] or "report")
    tpl = open(os.path.join(HERE, "ldo_report_template.html"), encoding="utf-8").read()
    title = "%s · %s" % (m["cell"], m["history"])
    page = (tpl.replace("{{TITLE}}", html.escape(title))
               .replace("/*{{DATA}}*/null", json.dumps(data, separators=(",", ":"))
                        .replace("</", "<\\/")))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    n = len(data["points"])
    print("ldo_report: %d points (%d with curves) from %s" % (n, n - len(m["missing"]), m["raw"]))
    print("ldo_report: wrote file://%s" % os.path.abspath(out))


if __name__ == "__main__":
    main()
