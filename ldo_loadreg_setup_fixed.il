;; ====================================================================
;; ldo_loadreg_setup.il
;; Adds an LDO load-regulation test to an existing ADE XL / ADE Assembler
;; (maestro) setup whose testbench schematic already has the LDO hooked up.
;;
;; How to use
;;   1. Edit the values in sections 1 to 3 below.
;;   2. Close the setup view in the GUI (or open it read-only) so this
;;      script can open it for editing.
;;   3. In the CIW:   load("ldo_loadreg_setup.il")
;;
;; Testbench assumptions
;;   - A DC current sink from DUT_OUTPUT to ground, value = ILOAD_VAR
;;   - A DC voltage source on DUT_POWER, value = VIN_VAR
;;   - Output cap and ESR already on the schematic
;;
;; Needs the mae* SKILL API: IC6.1.8 with ADE Assembler, or IC23 / IC25.
;; mae* cannot open a legacy ADE XL (data.sdb) view; build the test in an
;; Assembler (maestro) view. ldo_pvt_corners.il works on both.
;; Not tested on your install; check the printed DC analysis settings.
;; ====================================================================
ldoLoadregDir = let((f) f = get_filename(piport)
                  if(f && rindex(f "/") substring(f 1 strlen(f) - strlen(rindex(f "/"))) "."))
let((
    ;; ---------- 1. Where the setup lives ----------
    (LIB        "myLib")        ; library holding the adexl / maestro view
    (CELL       "tb_ldo")       ; testbench cell
    (VIEW       "adexl")        ; "adexl" or "maestro"
    (TB_VIEW    "schematic")    ; testbench schematic view
    (TEST       "LDO_LoadReg")  ; name of the test this script adds

    ;; ---------- 2. Net and instance names ----------
    (DUT        "I0")           ; $DUT        LDO instance in the testbench
    (DUT_POWER  "VIN")          ; $DUT_POWER  net on the LDO input pin
    (DUT_GND    "GND")          ; $DUT_GND    net on the LDO ground pin
    (DUT_OUTPUT "VOUT")         ; $DUT_OUTPUT net on the LDO output pin
    (ILOAD_VAR  "iload")        ; design variable on the DC load sink
    (VIN_VAR    "vin")          ; design variable on the VIN source

    ;; ---------- 3. Operating point and spec limits ----------
    (VIN_VAL      "1.8")
    (VOUT_NOM     "1.2")
    (ILOAD_MIN    "1m")
    (ILOAD_MAX    "300m")
    (NPTS         "31")         ; number of steps; the sweep has NPTS+1 points
    (SPEC_LOADREG "20")         ; load regulation, mV/A, max
    (SPEC_ERR     "1.5")        ; VOUT error over load, %, max
    (PVT          t)            ; t = add the shared PVT corners (ldo_pvt_corners.il)
    (TEMPS        "-40 25 125") ; corner temperatures; VIN corners are VIN_VAL -10% / +10%
    (RUN          t)            ; t = set up and run, nil = set up only

    sess vs vp dcOpts outs hist ok pvtFile)

  ;; ---------- open the setup ----------
  sess = maeOpenSetup(LIB CELL VIEW ?mode "a")
  unless(sess
    error("ldo_loadreg: cannot open %s/%s/%s for editing. Close it in the GUI and retry. A legacy ADE XL view (data.sdb) cannot be opened by mae* (ASSEMBLER-8036); use an ADE Assembler (maestro) view.\n"
          LIB CELL VIEW))
  when(member(TEST maeGetSetup(?session sess))
    error("ldo_loadreg: test %s already exists. Change TEST or delete that test first.\n" TEST))

  ;; ---------- test and design variables ----------
  maeCreateTest(TEST ?lib LIB ?cell CELL ?view TB_VIEW ?simulator "spectre" ?session sess)
  maeSetVar(VIN_VAR    VIN_VAL   ?session sess)
  maeSetVar("VOUT_NOM" VOUT_NOM  ?session sess)
  maeSetVar(ILOAD_VAR  ILOAD_MIN ?session sess)

  ;; ---------- DC sweep of the load current ----------
  ;; "sweep"/"designVar" make it a design-variable sweep, and "incrType" plus
  ;; "stepTypeLin" make "lin" a step count. Without them ADE stores the keys but
  ;; netlists a bare op point (no param=/start=/stop= on the dc line).
  dcOpts = `(("saveOppoint" t) ("sweep" "Design Variable")
             ("designVar" ,ILOAD_VAR) ("param" ,ILOAD_VAR)
             ("rangeType" "Start-Stop") ("start" ,ILOAD_MIN) ("stop" ,ILOAD_MAX)
             ("incrType" "Linear") ("stepTypeLin" "Number of Steps") ("lin" ,NPTS))
  ;; argument order changed between releases, so try both forms
  ok = errset(maeSetAnalysis(TEST "dc" ?enable t ?options dcOpts ?session sess))
  unless(ok && car(ok)
    ok = errset(maeSetAnalysis("dc" ?enable t ?options dcOpts ?session sess)))
  unless(ok && car(ok)
    printf("ldo_loadreg: could not set the DC sweep. Set it by hand: sweep %s from %s to %s.\n"
           ILOAD_VAR ILOAD_MIN ILOAD_MAX))
  printf("ldo_loadreg: DC analysis now reads %L\n"
         car(errset(maeGetAnalysis(TEST "dc" ?session sess))))

  ;; ---------- outputs and specs ----------
  vs = sprintf(nil "VS(\"/%s\")" DUT_OUTPUT)
  vp = sprintf(nil "VS(\"/%s\")" DUT_POWER)
  outs = list(
    list("VOUT"              vs nil nil)
    list("VOUT_light"        sprintf(nil "value(%s %s)" vs ILOAD_MIN) nil nil)
    list("VOUT_heavy"        sprintf(nil "value(%s %s)" vs ILOAD_MAX) nil nil)
    list("load_reg_mV_per_A" sprintf(nil "1000*(value(%s %s) - value(%s %s))/(%s - %s)"
                                     vs ILOAD_MIN vs ILOAD_MAX ILOAD_MAX ILOAD_MIN)
                             "lt" SPEC_LOADREG)
    list("VOUT_err_pct"      sprintf(nil "100*ymax(abs(%s - VAR(\"VOUT_NOM\")))/VAR(\"VOUT_NOM\")" vs)
                             "lt" SPEC_ERR)
    list("headroom_V"        sprintf(nil "value(%s - %s %s)" vp vs ILOAD_MAX) nil nil))
  foreach(o outs
    maeAddOutput(nth(0 o) TEST ?expr nth(1 o) ?session sess)
    when(nth(2 o)
      maeSetSpec(nth(0 o) TEST ?lt nth(3 o) ?session sess)))

  ;; ---------- shared PVT corners, enabled for every test ----------
  when(PVT
    pvtFile = strcat(ldoLoadregDir "/ldo_pvt_corners.il")
    unless(isFile(pvtFile)
      error("ldo_loadreg: PVT is t but %s is missing. Put ldo_pvt_corners.il beside this file.\n" pvtFile))
    ldoPvtLibOnly = t
    load(pvtFile)
    ldoPvtLibOnly = nil
    ldoPvtCorners(axlGetMainSetupDB(sess)
                  maeGetEnvOption(TEST ?option "modelFiles" ?session sess)
                  ?vinVar VIN_VAR ?vinTyp evalstring(VIN_VAL) ?temps TEMPS))

  ;; ---------- save, then optionally run ----------
  maeSaveSetup(?lib LIB ?cell CELL ?view VIEW ?session sess)
  printf("ldo_loadreg: test %s saved in %s/%s/%s (DUT %s, GND net %s).\n"
         TEST LIB CELL VIEW DUT DUT_GND)
  when(RUN
    hist = maeRunSimulation(?session sess)
    maeWaitUntilDone('All)
    printf("ldo_loadreg: run finished, history %L. Open the view to see pass/fail per corner.\n" hist)
    errset(maeExportOutputView(?session sess
                               ?fileName strcat("./" TEST "_results.csv")
                               ?view "Detail")))
  sess)
