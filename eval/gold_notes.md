# Gold set notes (eval/gold.json)

How it was made. I read the transcripts directly, without any extracted facts or LLM calls. For each team I dumped every turn of 4+ words with its turn number and read the kickoff fully, then the other meetings in full (IS1001 b/c/d, ES2012 a–d, ES2013 b/c/d, IS1005 a–c). For IS1004 and ES2005 I read the long meetings by skimming the first 450–900 characters of every turn and reading the decision and costing stretches in full. `eval/build_gold.py` holds the answers. Each quote is given as (meeting, turn, exact phrase) and copied out with `quote_from_span`. Running `eval/validate_gold.py` checks all 58 quotes with `verify_quote` and confirms each one sits in the turn it names. Turn numbers below are `turn_idx`.

Teams used: IS1001, ES2012, ES2013, IS1005, IS1004, ES2005. I did not use IS1006, ES2015, IS1000, TS3005 or TS3008.

## G01 IS1001 decision_change: voice control
- b t172–187: C says a standard remote takes about one month and speech recognition about eight months (t183). A drops speech at t343, and LCD at t340.
- c t2: A's recap says "no L_C_D_ no voice features". At t23–35, C reports the manufacturing email about an existing voice-recognition chip, and A defers the decision.
- d t75: C asks whether voice is in and answers himself "No". It is never raised again, and the d evaluation lists only the wheel and rubber as innovations (t66–72).
- Ambiguity: the "No" in d is informal, but nothing in d suggests voice was added.

## G02 IS1001 number: wheel sensor
- c t9–14: the presenter (B) says the wheel sensor costs 50 times a push button. A asks if it matters (t16, misstating the limit as "twelve Euros") and keeps the wheel at t172.
- a t63 sets the limit at 12.5. d t13–15: A reports the cost as 10.7 Euros, which includes the wheel.
- Caution: B and C seem to swap roles between b and c. In b, C presents the chip and components and B says "I'm designing the user interface" (t82). In c, B presents the chip and wheel sensor and C presents the UI and reports the voice-chip email. So the question avoids naming who gave the warning.

## G03 ES2012 reversal_count: custom vs standard chip
- c t0: the PM asks whether to go custom or off-the-shelf. t6–10: B says the voice tech is experimental, and A concludes that this "constrain[s] us to an existing chip". t32–53: the team keeps voice.
- c t288–289: D asks "And we're having a custom chip?" and A answers "We're having a a custom chip...".
- d t57: inside a long costing turn, A says they "will have to find a regular standard chip" and keeps the voice sensor.
- Counting: I call the c-t10 position a lean, not a decision, so key points ask only for the end-of-c state, the final state and the meeting of the last reversal (d), not a count.

## G04 ES2012 who: keeping voice recognition
- a t3/t5/t8 give the roles: D Andrew (Market Research), C Kendra (UI Designer), B Katie (Industrial Designer). b t98: the PM calls D "Andy".
- c t32: A suggests sticking to buttons. t41–44: C says "go for it". t48–50: D gives the argument that technological innovation is the second most important aspect to users. t53: A agrees, with up to five backup buttons.
- d t57: the voice sensor stays and the costing comes in "exactly on target at twelve point five". d t77 also mentions "the voice technology".
- Note: A says in d t57 that he treated the costs "slightly liberally". The 12.5 figure is what he states.

## G05 ES2012 decision_change: illumination
- a t51: the PM raises a remote that lights up, and B, C and D like it. b t2 is the PM's recap: "illuminated so that people can see it in dark rooms".
- c t297–303: D asks whether it will be lit from inside. A says no, because there are so few buttons (t298) and it "would clobber the battery life" (t303). C and B agree.
- Not raised in d.

## G06 ES2013 commitment_followup: rechargeable battery
- a t84 assigns the roles. It is garbled, but b t250 and c t251 confirm Kate does components and the industrial side (B), Steph the UI (C), and Sarah marketing (D, called "Sarah" in c t5).
- b t132/t154: C proposes a rechargeable internal battery. t160–162: D and A discuss a dock. t140: B says "I'll do some research for the next meeting."
- c: the rechargeable idea never comes back (grep "recharg|dock" finds nothing in c or d). t84: B says manufacturing gave no prices. t191–199: B has "no costing information" and names standard battery or kinetic, and the group goes kinetic.
- d t188–195: the spreadsheet uses a single kinetic source.

## G07 ES2013 commitment_followup: plastic shell plus spongy cover
- c t224–227: C proposes a hard plastic inner shell with a spongy cover. t231: B says she would need to ask manufacturing.
- d t26: B says the case can be spongy rubber or hard plastic and they are "not absolutely sure about a combination". t209–212: C says plastic, and B "still quite like[s]" combining. t228–232: A adds rubber as the case material supplement. t234: total 9.20 ("nine Pounds twenty") of 12.5.
- Ambiguity: the currency is spoken as Pounds while the budget is in Euros everywhere else (b t47). The key point records only the numbers.

## G08 ES2013 decision_change: titanium
- c t45–57: B lists the materials and says "I was thinking titanium myself." (t47, 5 words, cannot be cited). C says "I was thinking titanium" (t48), and D says "we could keep it titanium" (t57).
- c t129–132: C says titanium is too expensive and too "spacey", and B says it cannot be double-curved. t220: the PM restates "too expensive" and "won't do this double curves". t221–222: they choose rubber for sponginess.
- d t8 (recap): "plastic with a rubber casing". d costing: plastic plus a rubber supplement, single curve.

## G09 IS1005 who: radio vs infrared
- a t2/t6/t9/t7: B is Petre ("the technic", later the Industrial Designer, t92–102), C is Hamed (Interface Designer), D is Bob Morris (Marketing), A is Fabien (PM). a t81: C prefers radio waves.
- b t63: B says RF circuit prices are high. t80: personal preference RF, but on price he would stick to IR. t86–87: about 3 to 5 times dearer, and D says it is not worth the money.
- c t53: B says the company has developed an IR chip. t190–191: A says "we decided it's infra-red" and C agrees.

## G10 IS1004 who: solar cells
- a t3–7: A is Sebastian (PM), B Michael (UI), D Guillermo (Marketing), C Hemant (ID).
- c t207 (C): batteries, rechargeable via a base station, plus solar cells. c t297–304 covers the base station.
- d t58 (C) still lists solar cells and rechargeable batteries. t75–83: B says solar is not necessary with a recharging base, and C agrees ("now we are having rechargeable batteries").
- d t355–363: A forgot solar, B says "we decided against the solar cells", and A settles on one battery.

## G11 IS1004 other: sample sensor/speaker
- b t233/t243–249: the PM rules out speech recognition because he is not confident and 95% accuracy is "not good enough".
- c t105–139: B describes the in-house sample sensor and speaker (phrase match and playback, used on a coffee machine; every phrase must be trained, t129). He calls it not mature enough (t139). t185–202: it might do an "I'm here" reply, but a beep is enough.
- d t376–378: C enters the sample speaker in the spreadsheet, and B says they use only a simple beep. The base-station locator button is at d t45.
- Who presented the sample sensor in c: Speaker B (Michael).

## G12 ES2005 decision_change: speech recognition near-drop
- b t128: A says "as the Industrial Designer". C runs the meetings as PM, B is Raj (Marketing) and D is the UI and look designer.
- b t254: the PM says the remote-finder idea "takes out the speech recognition" (interference, expense). t291–294: "we're gonna scratch the speech recognition ... um expensive, no?" t295–297: A says it is much cheaper than the TFT and small-vocabulary. t300: vote for speech recognition, no TFT.
- c t82–86: D reports the technical department's speech breakthrough, and "we're definitely going for the speech recognition". c t406–413: the cheapest chip still covers buttons and speech.
- d t29: the prototype is "mainly speech recognition". t260: A confirms speech technology in the evaluation.

## G13 ES2005 number: 14.60 to 11.60
- a t33 and c t91: the limit is 12.50.
- d t69–71: C says 14.6 "minus the extra scroll buttons". t78–80: flattening is suggested, then "Doesn't save us quite as much". t83–105: they notice the double curve was entered twice ("It's meant to be one"). t107: "take away three". t112: "Eleven uh eleven Euros sixty" (4 words, not citable). t116: not quite enough to add the scroll wheel back. t130: "under twelve Euros fifty".
- Ambiguity: whether the final shape was flattened is never said, so no key point depends on it. "minus the extra scroll buttons" means the 14.6 already excludes them.

## G14 IS1005 false_premise: final costing
- IS1005 has only meetings a, b and c. I grepped all three for cost, euro, price, cheap, expensive, budget and twelve. The only figures are the 25 Euro selling price, the 50M profit, the 12.50 maximum production cost (a t50, b t19) and B's "three to five" times for RF. No total is ever computed.
- c t253–256: speech recognition is added at the end. c t262: the next tasks are look-and-feel and evaluation, which never happen in the corpus.

## Least certain
- G03: whether the c-t10 position counts as a "choice" (handled by not asking for a count).
- G02 and G01 for IS1001: the B/C role swap between meetings. The answers avoid letters, but a grader may see an answer naming C for the voice email and B for the wheel warning, and both are correct for meeting c.
- G06: the phrase "she came back with" may push agents to invent figures. Gold says plainly that there were none.
