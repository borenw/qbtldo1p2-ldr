;; ====================================================================
;; ldo_pvt_corners.il
;; Adds the LDO LDR PVT corner set to an ADE XL (adexl) or ADE Assembler
;; (maestro) setup and enables it for every test in that setup, so all
;; 15 checks run against the same corners.
;;
;;   Process  TT SS FF SF FS   (model sections swapped per corner)
;;   Voltage  VIN_TYP x (0.9 1 1.1)
;;   Temp     -40 25 125 C
;;   = 5 corners, 5 x 3 x 3 = 45 points
;;
;; How to use
;;   1. Edit section 1 below.
;;   2. Close the setup view in the GUI so this script can open it.
;;   3. In the CIW:   load("ldo_pvt_corners.il")
;;
;; ldo_loadreg_setup.il calls ldoPvtCorners() itself when PVT is t.
;; Checks that sweep VIN (#2 Dropout, line_reg in #8) or temperature
;; (#13 Thermal shutdown) cannot also take that corner variable: pass
;; ?vary '(P T) or '(P V) and ?tests for those, with a ?suffix so the
;; names do not clash. See the table on the LDR page.
;; ====================================================================

;; ---------- process corners: name, then section swaps ----------
;; Each swap is (nominal section  corner section). Sections not listed
;; stay nominal. This model file has no ss/ff-only resistor or MIM split
;; for SF/FS, so those keep tt_res / tt_mim.
ldoPvtProcess = '(
  ("TT" ("tt" "tt") ("tt_3v" "tt_3v"))
  ("SS" ("tt" "ss") ("tt_3v" "ss_3v") ("tt_res" "ss_res") ("tt_mim" "ss_mim"))
  ("FF" ("tt" "ff") ("tt_3v" "ff_3v") ("tt_res" "ff_res") ("tt_mim" "ff_mim"))
  ("SF" ("tt" "sf") ("tt_3v" "sf_3v"))
  ("FS" ("tt" "fs") ("tt_3v" "fs_3v")))

;; sdb        setup DB handle: axlGetMainSetupDB(session)
;; models     the test's nominal model list, (("file" "section") ...)
;; tests      tests to enable the corners for; nil = every test in sdb
;; vary       which of P V T the corners change
procedure(ldoPvtCorners(sdb models @key (vinVar "vin") (vinTyp 3.3) (vinTol 0.1)
                        (temps "-40 25 125") (vary '(P V T)) (tests nil) (suffix ""))
  let((allTests names c m mf sec swap vinList i)
    allTests = cadr(axlGetTests(sdb))
    unless(tests tests = allTests)
    vinList = sprintf(nil "%g %g %g" vinTyp*(1-vinTol) vinTyp vinTyp*(1+vinTol))
    foreach(p ldoPvtProcess
      c = axlPutCorner(sdb strcat(car(p) suffix))
      axlSetEnabled(c t)
      names = cons(strcat(car(p) suffix) names)
      when(memq('T vary) axlPutVar(c "temperature" temps))
      when(memq('V vary) axlPutVar(c vinVar vinList))
      i = 0
      foreach(fs models
        sec = cadr(fs)
        when(memq('P vary) && (swap = assoc(sec cdr(p))) sec = cadr(swap))
        mf = car(last(parseString(car(fs) "/")))
        m = axlPutModel(c if(i == 0 mf sprintf(nil "%s:%d" mf i)))
        axlSetModelFile(m car(fs))
        axlSetModelSection(m sprintf(nil "\"%s\"" sec))
        axlSetModelTest(m "All")
        axlSetModelBlock(m "Global")
        axlSetEnabled(m t)
        i++)
      foreach(tn allTests axlSetCornerTestEnabled(c tn if(member(tn tests) t nil))))
    printf("ldoPvtCorners: %L, vin %s, temp %s, vary %L, tests %L\n"
           reverse(names) vinList temps vary tests)
    reverse(names)))

;; Legacy ADE XL keeps each test's model list in
;; <view>/test_states/<lib>/<cell>/<sim>/<test>_active/modelSetup
procedure(ldoPvtSubdirs(d)
  setof(x getDirFiles(d) !member(x '("." "..")) && isDir(strcat(d "/" x))))
procedure(ldoPvtStateModels(viewDir test)
  let((root f p models)
    root = strcat(viewDir "/test_states")
    foreach(l ldoPvtSubdirs(root)
      foreach(c ldoPvtSubdirs(strcat(root "/" l))
        foreach(sim ldoPvtSubdirs(strcat(root "/" l "/" c))
          f = strcat(root "/" l "/" c "/" sim "/" test "_active/modelSetup")
          when(!models && isFile(f)
            p = infile(f)  models = car(lineread(p))  close(p)))))
    models))

;; ---------- standalone run ----------
;; Skipped when ldo_loadreg_setup.il loads this file for the procedure.
unless(boundp('ldoPvtLibOnly) && ldoPvtLibOnly
let((
    ;; ---------- 1. Where the setup lives and the operating point ----------
    (LIB      "myLib")
    (CELL     "tb_QbtLdo1p2_LoadReg")
    (VIEW     "adexl")     ; "adexl" or "maestro"
    (VIN_VAR  "vin")       ; design variable on the VIN source
    (VIN_TYP  3.3)         ; typical VIN; corners use -10% / typ / +10%
    (TEMPS    "-40 25 125")
    sess sdb tests models axl)

  axl = (VIEW == "adexl")
  if(axl then
    ;; legacy ADE XL: maeOpenSetup refuses data.sdb (ASSEMBLER-8036)
    sess = axlCreateSession(sprintf(nil "ldoPvt%d" random(100000)))
    sdb = car(errset(axlSetMainSetupDBLCV(sess LIB CELL VIEW)))
  else
    sess = maeOpenSetup(LIB CELL VIEW ?mode "a")
    sdb = sess && axlGetMainSetupDB(sess))
  unless(sdb
    error("ldoPvtCorners: cannot open %s/%s/%s for editing. Close it in the GUI and retry.\n"
          LIB CELL VIEW))
  tests = cadr(axlGetTests(sdb))
  unless(tests error("ldoPvtCorners: %s/%s/%s has no tests.\n" LIB CELL VIEW))
  ;; nominal models come from the first test; every test in one setup
  ;; normally shares the PDK model list
  models = if(axl
    ldoPvtStateModels(ddGetObj(LIB CELL VIEW)~>readPath car(tests))
    maeGetEnvOption(car(tests) ?option "modelFiles" ?session sess))
  unless(models error("ldoPvtCorners: test %s has no model files.\n" car(tests)))
  ldoPvtCorners(sdb models ?vinVar VIN_VAR ?vinTyp VIN_TYP ?temps TEMPS)
  if(axl then
    axlCommitSetupDB(sdb)
    axlCloseSetupDB(sdb)
    axlCloseSession(sess)
  else
    maeSaveSetup(?session sess)
    maeCloseSession(?session sess))
  printf("ldoPvtCorners: saved %s/%s/%s. Reopen it and check the Corners setup.\n" LIB CELL VIEW)
  t))
