;; ldr_viva_figs.il - export the load-regulation figures from Virtuoso Visualization (ViVA).
;; ldr_page.py writes ldr_points.il (ldoRaw ldoTest ldoOut ldoVinTyp ldoVnom ldoErr ldoVins
;; ldoYlim ldoPts) and then runs:   virtuoso -nograph < ldr_viva_figs.il
;; Each point is (pointID corner process vin temperature); its PSF is
;; <ldoRaw>/<pointID>/<ldoTest>/psf.
;; One subwindow per panel, each saved as its own PNG (<name>_<n>.png); ldr_page.py
;; stacks them. Red (y1) is kept for spec lines, so the processes use y2..y6.

load("ldr_points.il")

ldoColor = '(("TT" "y6") ("SS" "y2") ("FF" "y3") ("SF" "y4") ("FS" "y5"))
ldoStyle = '((-40 "dash") (25 "solid") (125 "dot"))

;; spec lines: thick red flat traces, named with the limit in the legend
procedure(ldoSpecLines(win sub ref)
  foreach(sp list(list(1 "<=" "+") list(-1 ">=" "-"))
    let((y lab)
      y = ldoVnom*(1 + car(sp)*ldoErr/100.0)
      lab = sprintf(nil "SPEC %s %.3f V" cadr(sp) y)
      ;; pure red (y1) is reserved for spec lines; ldr_page.py finds these rows and
      ;; writes the value and criterion beside them
      awvPlotWaveform(win list(ref*0 + y) ?subwindow sub ?expr list(lab)
                      ?color list("y1") ?lineStyle list("solid") ?lineThickness list("thick")))))

procedure(ldoPlotPoints(win pts sub)
  let((w ref)
    foreach(p pts
      openResults(sprintf(nil "%s/%d/%s/psf" ldoRaw car(p) ldoTest))
      w = v("VOUT" ?result 'dc)
      ref = w
      awvPlotWaveform(win list(w) ?subwindow sub
                      ?expr list(sprintf(nil "%s %gV %dC" nth(2 p) nth(3 p) nth(4 p)))
                      ?color list(or(cadr(assoc(nth(2 p) ldoColor)) "y7"))
                      ?lineStyle list(or(cadr(assoc(nth(4 p) ldoStyle)) "solid"))
                      ?lineThickness list("medium")))
    when(ref ldoSpecLines(win sub ref))
    length(pts)))

;; save every subwindow of win as <prefix>_<n>.png
procedure(ldoSave(win prefix h)
  saveGraphImage(?window win ?fileName strcat(ldoOut "/" prefix ".png") ?width 1400 ?height h
                 ?units "pixels" ?backgroundColor "white" ?saveEachSubwindowSeparately t))

;; ---- figure A: typical VIN, every process x temperature ----
let((win n)
  win = awvCreatePlotWindow()
  n = ldoPlotPoints(win setof(p ldoPts abs(nth(3 p) - ldoVinTyp) < 1e-6) 1)
  awvSetXAxisLabel(win "ILOAD" ?subwindow 1)
  awvSetYAxisLabel(win 1 sprintf(nil "VOUT, VIN %g V" ldoVinTyp) ?subwindow 1)
  printf("LDR fig_typ %d curves -> %L\n" n ldoSave(win "fig_loadreg_typ" 800))
  t)

;; ---- figure B: every point, one subwindow per VIN ----
let((win i n)
  win = awvCreatePlotWindow()
  i = 0  n = 0
  foreach(vv ldoVins
    i++
    when(i > 1 awvAddSubwindow(win))
    n = n + ldoPlotPoints(win setof(p ldoPts abs(nth(3 p) - vv) < 1e-6) i)
    awvSetXAxisLabel(win "ILOAD" ?subwindow i)
    awvSetYAxisLabel(win 1 sprintf(nil "VOUT, VIN %g V" vv) ?subwindow i)
    ;; clip a panel whose collapsed corners would flatten the rest
    when(member(vv ldoYlim) awvSetYLimit(win 1 list(ldoVnom*0.72 ldoVnom*1.22) ?subwindow i)))
  printf("LDR fig_pvt %d curves -> %L\n" n ldoSave(win "fig_loadreg_pvt" 560))
  t)

printf("LDR DONE\n")
hiQuit()
