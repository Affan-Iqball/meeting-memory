"""Build eval/gold.json from hand-written gold answers.

Gold answers and key points were written by reading the transcripts (see
eval/gold_notes.md). Evidence quotes are NOT typed by hand: each one is given
as (meeting, turn, phrase) and the exact words are copied out of the turn with
meetmem.cite.quote_from_span, using the word range where `phrase` occurs.

Run:  python3 eval/build_gold.py   (then python3 eval/validate_gold.py)
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meetmem.cite import quote_from_span  # noqa: E402
from meetmem.search import get_turns  # noqa: E402


def ev(meeting_id, turn_idx, phrase, supports):
    """Locate `phrase` (raw words, exact) inside one turn and copy that span."""
    rows = get_turns(meeting_id, turn_idx, turn_idx)
    if not rows:
        raise SystemExit(f"no turn {meeting_id} t{turn_idx}")
    words = rows[0]["text"].split()
    pw = phrase.split()
    for s in range(len(words) - len(pw) + 1):
        if words[s:s + len(pw)] == pw:
            q = quote_from_span(meeting_id, turn_idx, s, s + len(pw))
            return {"meeting_id": meeting_id, "turn_idx": turn_idx, "quote": q, "supports": supports}
    raise SystemExit(f"phrase not found in {meeting_id} t{turn_idx}: {phrase!r}\n  turn: {rows[0]['text'][:300]!r}")


Q = []


def add(**kw):
    kw["question_id"] = f"G{len(Q) + 1:02d}"
    order = ["question_id", "team", "type", "question", "gold_answer", "key_points", "evidence",
             "must_cite_meetings", "difficulty_notes"]
    Q.append({k: kw[k] for k in order})


# ---------------------------------------------------------------- IS1001
add(team="IS1001", type="decision_change",
    question="Team IS1001 ruled out voice control for their remote early on. What reason was given, what news later reopened the question, and was voice control in the final product?",
    gold_answer="In the functional design meeting they dropped speech/voice control because it would take about eight months to develop (against about one month for a standard remote) and delay getting to market. In the conceptual meeting a member reported an email from the manufacturing division saying a voice recognition chip was already developed, but the final product did not include voice control.",
    key_points=["dropped because development would take about eight months / delay the product",
                "later news: manufacturing division already had a voice recognition chip",
                "final product: no voice control"],
    evidence=[ev("IS1001b", 183, "I would say uh about eight months to have the first results.", "about eight months"),
              ev("IS1001b", 343, "And so no speech as well because it w it would delay too much the development process.", "dropped because of delay"),
              ev("IS1001c", 27, "an email from the manufacturing division that they have basically a voice recognition chip already developed.", "manufacturing has a chip"),
              ev("IS1001d", 75, "are we including the voice in the end or not? Huh? No. Okay.", "final: no voice")],
    must_cite_meetings=["IS1001b", "IS1001c", "IS1001d"],
    difficulty_notes="Three meetings. The reopening (chip email) is never formally closed: the final 'no' is a half-sentence answered by a head-shake ('Huh? No. Okay.') in the detailed design meeting. Speakers B and C appear to swap the industrial/UI presenter roles between meetings b and c, so do not rely on letters for role.")

add(team="IS1001", type="number",
    question="Team IS1001 was told that one of the controls they wanted cost far more than an ordinary button. Which control was it, how much more did it cost, did they keep it, and how did the final production cost compare with their limit?",
    gold_answer="The wheel sensor (for changing channels) was said to cost about fifty times as much as a push button. The project manager kept the wheel anyway because it was easy and innovative, and the final production cost came to 10.7 Euros, under the 12.5 Euro limit.",
    key_points=["the expensive control was the wheel (wheel sensor)",
                "about fifty times the price of a push button",
                "the wheel was kept",
                "final cost 10.7 Euros against a 12.5 Euro limit"],
    evidence=[ev("IS1001c", 14, "Uh a wheel sensor is fifty time the price of a a a push button.", "fifty times a push button"),
              ev("IS1001c", 172, "So, I think we can keep the wheel because it's uh easy, it's innovative, even if the cost is a bit higher,", "wheel kept"),
              ev("IS1001d", 15, "And the cost ended to be ten point seven Euros. So which is uh good, because we had a price gap of twelve point five Euros.", "10.7 vs 12.5"),
              ev("IS1001a", 63, "to have a production cost lower than twelve point five Euros", "12.5 limit")],
    must_cite_meetings=["IS1001c", "IS1001d"],
    difficulty_notes="Numbers are spelled out ('fifty time', 'ten point seven', 'twelve point five'). In meeting c the project manager misstates the limit as 'up to twelve Euros'; the limit is 12.5 in meetings a and d. The wheel was proposed in meeting b; the cost warning comes in c.")

# ---------------------------------------------------------------- ES2012
add(team="ES2012", type="reversal_count",
    question="Team ES2012 went back and forth on whether their remote needed a purpose-built chip or an ordinary one. What did they settle on at the end of their conceptual design meeting, what did the final costing use, and in which of their meetings was that last reversal made?",
    gold_answer="Early in the conceptual design meeting the project manager leaned towards an existing chip with plain buttons, but once the team kept voice recognition he summed up that they were having a custom chip. In the fourth (final, detailed design) meeting he reversed this himself: the costing uses a regular standard chip, while the expensive voice sensor stays.",
    key_points=["end of the conceptual (third) meeting: custom chip",
                "final costing: a regular standard (off-the-shelf) chip",
                "last reversal made in the fourth / final meeting, by the project manager",
                "voice recognition (voice sensor) still kept in the final design"],
    evidence=[ev("ES2012c", 10, "that would appear to uh effectively constrain us to an existing chip and thus therefore conventional button technology.", "early lean: existing chip"),
              ev("ES2012c", 289, "We're having a a custom chip, but given the the we've cut the functions down,", "end of c: custom chip"),
              ev("ES2012d", 57, "we're we will have to find a s a regular standard chip to to do this with", "final: standard chip"),
              ev("ES2012d", 57, "The uh the the voice sensor is is expensive but we we made a a basic decision that that was absolutely fundamental to the to the design so that that has to stay.", "voice sensor kept")],
    must_cite_meetings=["ES2012c", "ES2012d"],
    difficulty_notes="Two changes, the first inside one meeting (existing chip -> custom chip in c), the second buried mid-way through a very long project-manager turn while filling in the cost spreadsheet in d, with no discussion. The custom-chip confirmation in c is a 5-word question by D ('And we're having a custom chip?') answered by A.")

add(team="ES2012", type="who",
    question="When Team ES2012's project manager suggested falling back on buttons only because voice technology was risky, who pushed to keep voice recognition, who backed them up and with what argument, and did it survive into the final design?",
    gold_answer="Kendra, the User Interface Designer (Speaker C), said she was inclined to just go for it, and Andrew, the market research / marketing person (Speaker D), backed her by pointing out that being technologically innovative was the second most important thing to users. Voice recognition was kept (with a few backup buttons) and in the final costing the expensive voice sensor stayed, with the total coming in exactly on the 12.5 target.",
    key_points=["Speaker C (Kendra, User Interface Designer) pushed to keep it",
                "Speaker D (Andrew, Marketing / market research) backed it: technological innovation is the second most important aspect to users",
                "voice recognition kept in the final design (voice sensor stayed in costing)",
                "final costing exactly 12.5"],
    evidence=[ev("ES2012a", 5, "I'm Kendra and I'm the Us User Interface Designer", "C is Kendra, UI designer"),
              ev("ES2012c", 44, "Anyway, I'm I'm incli kinda inclined to say that we should just go for it.", "C pushes for voice"),
              ev("ES2012c", 48, "it's the second most important aspect to users that the device should be technologically innovative.", "D's argument"),
              ev("ES2012d", 57, "the costings uh come in at exactly on target at twelve point five", "final 12.5")],
    must_cite_meetings=["ES2012a", "ES2012c", "ES2012d"],
    difficulty_notes="Names and roles are only given in the kickoff (ES2012a t3/t5/t8); later meetings use letters or first names (the PM calls D 'Andy'). The PM's objection and the team's push-back are in one stretch of meeting c; the outcome is only visible in d's cost turn.")

add(team="ES2012", type="decision_change",
    question="Team ES2012 initially wanted their remote to light up so it could be used in a dark room. What happened to that idea, who dropped it, and for what reasons?",
    gold_answer="Illumination was on the list from the first two meetings, but in the conceptual design meeting the project manager dropped it when the marketing member asked about lighting the buttons: with so few buttons it was redundant, and it would also badly drain the battery that was meant to last for years.",
    key_points=["first: remote should be illuminated / light up for dark rooms",
                "dropped in the conceptual (third) meeting by the project manager (Speaker A)",
                "reason 1: so few buttons that lighting is redundant",
                "reason 2: it would hurt battery life"],
    evidence=[ev("ES2012b", 2, "illuminated so that people can see it in dark rooms.", "first plan: illuminated"),
              ev("ES2012c", 298, "Um the that I mean that's no, because we've got so few buttons that it that actually makes that redundant.", "dropped: redundant"),
              ev("ES2012c", 303, "that solves one of the problems of b of battery life, 'cause that would well that would clobber the battery life,", "battery-life reason")],
    must_cite_meetings=["ES2012b", "ES2012c"],
    difficulty_notes="The drop happens in the last minutes of meeting c as an answer to D's half-question ('are we gonna have it il being illuminated from inside onto the buttons or'); 'that' in A's answer refers back to D's question. The original wish appears only in the PM's summary of meeting a at the start of b.")

# ---------------------------------------------------------------- ES2013
add(team="ES2013", type="commitment_followup",
    question="In Team ES2013, a team member said she would research whether an idea for powering the remote was affordable. Who was it, what did she come back with at the next meeting, and what energy source went into the final costing?",
    gold_answer="Kate, the Industrial Designer (Speaker B), said she would research the suggested rechargeable internal battery (with a dock) for the next meeting. At the conceptual design meeting she had no costing information (manufacturing had not attached prices) and named a standard battery or kinetic energy as the two front-runners; the team chose kinetic, and the final costing used one kinetic energy source.",
    key_points=["Speaker B (Kate, Industrial Designer) made the commitment, about a rechargeable internal battery",
                "she reported no cost figures / no costing information",
                "she named standard battery or kinetic as the front-runners",
                "final costing: kinetic energy"],
    evidence=[ev("ES2013a", 84, "the industrial design is the first one, that's Kate,", "B is Kate, industrial design"),
              ev("ES2013b", 140, "I don't know much about what these things cost. I'll do some research for the next meeting.", "commitment"),
              ev("ES2013c", 192, "Oh unfortunately I don't have costing information.", "no figures"),
              ev("ES2013c", 192, "I think the the two front-runners are the standard battery or the kinetic,", "front-runners"),
              ev("ES2013d", 195, "Kinetic is um and how many of those will we need per we only need one.", "final: kinetic")],
    must_cite_meetings=["ES2013b", "ES2013c", "ES2013d"],
    difficulty_notes="The follow-up is a non-answer: the rechargeable battery is never mentioned again, so an agent must notice that the commitment was not fulfilled rather than invent a figure. The idea itself was Steph's (Speaker C); the commitment is Kate's (B). The kickoff role assignment (ES2013a t84) is garbled.")

add(team="ES2013", type="commitment_followup",
    question="Team ES2013's industrial designer said she would ask manufacturing whether a suggested way of building the case was possible. What was the suggestion, what did she report in the final meeting, and what case material and total cost ended up in the final costing?",
    gold_answer="The suggestion (from Speaker C, Steph) was a hard plastic inner shell with a thick spongy rubber cover. In the final meeting Kate (Speaker B) reported that the case could be either spongy rubber or hard plastic, but they were not sure about combining the two. The final costing used plastic with a rubber case-material supplement and totalled 9.20 (said as 'nine Pounds twenty') against the 12.50 allowed.",
    key_points=["suggestion: hard plastic inner shell with a spongy rubber cover",
                "report: either rubber or plastic is possible, combination uncertain",
                "final costing: plastic case plus rubber supplement",
                "total 9.20 against an allowance of 12.50"],
    evidence=[ev("ES2013c", 224, "Do you not think it might need like a kind of a hard plastic inner shell with the actual delicate workings inside,", "suggestion"),
              ev("ES2013c", 231, "I'd need to talk to manufacturing again about whether that's actually possible,", "commitment"),
              ev("ES2013d", 26, "The the case can be either um spongy rubber or hard plastic. We're not absolutely sure about a combination of the two,", "report back"),
              ev("ES2013d", 232, "Um let's put that rubber in then, of the case material supplement.", "rubber supplement in costing"),
              ev("ES2013d", 234, "the total cost has been calculated as nine Pounds twenty out of the twelve and a half we were allowed.", "9.20 of 12.50")],
    must_cite_meetings=["ES2013c", "ES2013d"],
    difficulty_notes="The total is spoken as 'nine Pounds twenty' although the budget is in Euros elsewhere: an agent should report 9.20 and may flag the currency slip. The combination question is reported back as 'not absolutely sure', yet the costing then adds rubber on top of plastic anyway.")

add(team="ES2013", type="decision_change",
    question="Team ES2013 was attracted to a metal for the case before changing their minds. What was the metal, who was drawn to it, why was it rejected, and what did they choose instead?",
    gold_answer="Titanium: Kate (Speaker B) and Steph (Speaker C) both said they were thinking titanium, and the marketing member said they could keep it titanium. It was rejected because it was too expensive (and, for Steph, too futuristic for a colourful, organic look) and because a double-curved case cannot be made in titanium. They chose plastic with a rubber (spongy) covering instead.",
    key_points=["first leaning: titanium",
                "drawn to it: Speaker B (Kate) and Speaker C (Steph); D also agreed",
                "rejected: too expensive / too futuristic, and no double-curved case in titanium",
                "chosen instead: plastic with rubber (spongy) covering"],
    evidence=[ev("ES2013c", 48, "I was thinking titanium, I was thinking it's just I have been influenced by pictures of iPods,", "C drawn to titanium"),
              ev("ES2013c", 129, "I mean titanium would be great, but I think it might be a bit too expensive and too kinda spacey.", "too expensive"),
              ev("ES2013c", 132, "we can't for example have a double curved case in titanium unfortunately.", "no double curve"),
              ev("ES2013d", 8, "Um we would use plastic with a rubber casing, I think was the consensus,", "plastic with rubber")],
    must_cite_meetings=["ES2013c", "ES2013d"],
    difficulty_notes="Kate's own 'I was thinking titanium myself.' (ES2013c t47) is only five words, so it cannot be cited; her leaning must be inferred from it or from context. The rejection is spread over three turns by two speakers and restated by the PM (t220); the outcome is only summarised at the start of d.")

# ---------------------------------------------------------------- IS1005
add(team="IS1005", type="who",
    question="Team IS1005 debated whether the remote should talk to the TV by radio or by infrared. Who first preferred radio, who argued against it and why, and what did they finally use?",
    gold_answer="Hamed, the (User) Interface Designer (Speaker C), first said he preferred radio waves because infrared has to be pointed at the TV. Petre, the industrial/technical designer (Speaker B), said radio-frequency circuits were expensive (about three to five times the price) and preferred infrared, and the marketing expert Bob (Speaker D) agreed it was not worth the extra money. They finally chose infrared, using an infrared chip the company had already developed.",
    key_points=["Speaker C (Hamed, Interface Designer) first preferred radio waves",
                "Speaker B (Petre, industrial/technical designer) argued against it on price (about three to five times more expensive)",
                "Speaker D (Bob, Marketing) agreed it was not worth the extra money",
                "final: infrared, with the company's own infrared chip"],
    evidence=[ev("IS1005a", 9, "My name is Hamed Getabdar, and uh I'm going to be Interface Designer in this project.", "C is Hamed, interface designer"),
              ev("IS1005a", 81, "I prefer um remote control working with radio waves, because remote control working with infra-red rays you should you should you should keep it in a specific direction", "C prefers radio"),
              ev("IS1005b", 80, "but from the point of view uh of the design and price, I would stick to I_R_s.", "B prefers IR on price"),
              ev("IS1005b", 87, "I think it's it's probably not worth spending the extra money,", "D agrees"),
              ev("IS1005c", 53, "we decided to use the infra-red unit because our company has also developed the chip for communication by the infra-red,", "final: IR, own chip")],
    must_cite_meetings=["IS1005a", "IS1005b", "IS1005c"],
    difficulty_notes="Roles are muddled in the kickoff (Petre says he is 'the technic', the PM assigns 'technical functions' to Hamed); B's price ratio is split across turns ('three to five. N not ten times', t86) and D's question. B also says his personal preference would be RF before choosing IR, which a careless reader can flip. Team has only three meetings.")

# ---------------------------------------------------------------- IS1004
add(team="IS1004", type="who",
    question="Team IS1004 planned more than one power source for their remote. What combination was first proposed and by whom, who argued in the final meeting that part of it was unnecessary, and what went into the final costing?",
    gold_answer="Hemant, the Industrial Designer (Speaker C), proposed batteries (rechargeable on a base station) plus solar cells, and still listed both in the final prototype. In the final meeting Michael, the User Interface Designer (Speaker B), said the solar cells were no longer necessary given the recharging base station, and the final costing used a single (rechargeable) battery with no solar cells.",
    key_points=["first proposal: batteries (rechargeable via base station) plus solar cells",
                "proposed by Speaker C (Hemant, Industrial Designer)",
                "Speaker B (Michael, User Interface Designer) said solar cells were unnecessary",
                "final costing: battery only, no solar cells"],
    evidence=[ev("IS1004a", 4, "I'm uh Michael. I'm the user interface designer.", "B is Michael, UI"),
              ev("IS1004a", 7, "And I am Hemant, the industrial designer.", "C is Hemant, ID"),
              ev("IS1004c", 207, "So I was of the idea that we can have two kind of power supplies, one is the usual batteries which are there,", "C proposes batteries + solar"),
              ev("IS1004d", 81, "I don't I really don't think it's necessary to have the solar cells anymore.", "B drops solar"),
              ev("IS1004d", 363, "Yeah. Okay so we we stick to battery, one.", "final: battery only")],
    must_cite_meetings=["IS1004a", "IS1004c", "IS1004d"],
    difficulty_notes="Names/roles only in the kickoff. The solar cells survive into the prototype presentation of the final meeting (C, t58) and are dropped a few turns later by B, then confirmed only implicitly in the spreadsheet ('we decided against the solar cells', t357). The solar half of C's proposal is in the same long turn after the quoted part.")

add(team="IS1004", type="other",
    question="After Team IS1004 decided against speech recognition, a related voice technology the company already had was brought up. What was it, why was it judged unsuitable for controlling the TV, and what did the final remote use instead for its related function?",
    gold_answer="Speech recognition was ruled out in the functional meeting because the project manager was not confident it would work. In the conceptual meeting Michael (Speaker B) presented the company's in-house 'sample sensor, sample speaker', which only matches a trained phrase and plays back a reply (used on a coffee machine); it was judged not mature enough to be a speech recognition engine, at most useful for the remote to answer 'I'm here'. In the final costing they did not use the sample speaker and used a simple beep for finding the remote instead.",
    key_points=["speech recognition ruled out earlier (PM not confident / accuracy not good enough)",
                "in-house technology: sample sensor / sample speaker that matches a set phrase and plays a reply (coffee machine)",
                "unsuitable: needs every phrase trained, not mature enough as speech recognition",
                "final: no sample speaker, just a simple beep (remote locator)"],
    evidence=[ev("IS1004b", 233, "we don't go for speech recognition technology.", "ruled out"),
              ev("IS1004c", 111, "It means that it can recognize, it can do like a match on a on a certain phrase that you speak and then can play back a phrase in response to that.", "what the sample sensor/speaker does"),
              ev("IS1004c", 139, "it's it's not mature enough to use as a speech recognition engine,", "unsuitable"),
              ev("IS1004d", 378, "that sample speaker is not we we're not using that, we're just using the the very beep simple beep,", "final: beep")],
    must_cite_meetings=["IS1004b", "IS1004c", "IS1004d"],
    difficulty_notes="'Speech' is used loosely for three different things (speech recognition, sample sensor/speaker pattern matching, and a spoken 'I'm here' locator). The sample speaker is first entered in the final spreadsheet by C and removed a turn later by B, so a single snippet gives the wrong answer.")

# ---------------------------------------------------------------- ES2005
add(team="ES2005", type="decision_change",
    question="Team ES2005's project manager moved to drop speech recognition at one point. In which meeting was that, who objected and with what argument, and did speech recognition make it into the final prototype?",
    gold_answer="In the second (functional design) meeting the project manager (Speaker C) said they would scratch speech recognition as expensive. The Industrial Designer (Speaker A) objected that it was much cheaper than the TFT screen, just a microphone and some integrated circuits for a small-vocabulary system, and the vote went for speech recognition instead of the TFT. It stayed: the final prototype works mainly by speech recognition, helped by a speech-recognition breakthrough announced in the third meeting.",
    key_points=["near-drop in the second (functional design) meeting, by the project manager (Speaker C)",
                "objection by Speaker A (Industrial Designer): much cheaper than the TFT, just a microphone and some integrated circuits",
                "same meeting: vote for speech recognition, no TFT",
                "final prototype is mainly speech recognition"],
    evidence=[ev("ES2005b", 291, "Um I think we're gonna scratch the speech recognition as a bit of a", "PM moves to drop"),
              ev("ES2005b", 295, "Oh no it's it's much cheaper than the T_F_T_, it's just a microphone and some some integrated circuits.", "A objects"),
              ev("ES2005b", 300, "Who wants T_F_T_? No-one does. Excellent, so we'll go with speech recognition, yeah?", "vote"),
              ev("ES2005d", 29, "If you do need buttons, you can flip it over, and there's some there, um but mainly it's speech recognition.", "final prototype")],
    must_cite_meetings=["ES2005b", "ES2005d"],
    difficulty_notes="Reversal inside one meeting within ten turns, with the PM's 'scratch it' sentence cut off and continued in the next turn ('um expensive, no?'). A's role is stated only in b t128 ('as the Industrial Designer'). Earlier in b (t254) the PM also says a remote-finder 'takes out the speech recognition', which looks like a decision but is not.")

add(team="ES2005", type="number",
    question="Team ES2005's first cost total in their final meeting was over budget. What was the figure, what was the limit, what did the project manager first suggest to save money, and what final figure did they reach and how?",
    gold_answer="The first total was 14.60 Euros (already without the extra scroll buttons), against a 12.50 Euro limit. The project manager first suggested keeping the shape but flattening it, which did not save enough; they then found the double-curved case had been entered twice in the spreadsheet, and correcting it to one took off three Euros, giving 11.60 Euros, under the limit.",
    key_points=["first total 14.60 Euros (without the extra scroll buttons)",
                "limit 12.50 Euros",
                "first idea: flatten the shape (did not save enough)",
                "fix: double-curved case counted twice, corrected to one, minus 3 Euros",
                "final 11.60 Euros, under budget"],
    evidence=[ev("ES2005d", 71, "the unit we are currently going to produce minus the extra scroll buttons, uh it's gonna cost us fourteen point six Euros.", "14.60"),
              ev("ES2005a", 33, "remember we're trying to make it for twelve Euros fifty.", "12.50 limit"),
              ev("ES2005d", 78, "I was thinking that we could have essentially the same shape, but just have it flattened.", "flatten idea"),
              ev("ES2005d", 105, "It's meant to be one, yeah, I don't know why I put two in there,", "double curve entered twice"),
              ev("ES2005d", 130, "Okay, so project evaluation. We have under twelve Euros fifty.", "under budget")],
    must_cite_meetings=["ES2005a", "ES2005d"],
    difficulty_notes="The final figure 'Eleven uh eleven Euros sixty' (ES2005d t112) is only four words and cannot be quoted; it must be stated from reading. The real fix is a spreadsheet data-entry error, not a design change; the flattening idea sounds like the fix but the PM says it 'doesn't save us quite as much' (t80). Whether the final shape was flattened is never stated.")

# ---------------------------------------------------------------- false premise
add(team="IS1005", type="false_premise",
    question="Team IS1005's final costing came in under their production cost limit. What total did they reach, and which feature did they have to cut to get there?",
    gold_answer="The premise is false: Team IS1005 held only three meetings (kickoff, functional and conceptual design) and never did a costing, so there is no total and nothing was cut for cost. The only cost figure is the target of at most 12.50 Euros production cost per unit; at the end of the conceptual meeting they were adding features (LCD, speech recognition, solar cells plus batteries), not removing them.",
    key_points=["premise false: no costing / no total was ever produced",
                "nothing was cut to meet a budget",
                "only figure: production cost target of at most 12.50 Euros per unit",
                "end of last meeting: features such as speech recognition were added, not cut"],
    evidence=[ev("IS1005a", 50, "We expect a production cost of maximum uh twelve point fifty Euro.", "12.50 target"),
              ev("IS1005c", 256, "So we add also the speech recognition device.", "adding, not cutting")],
    must_cite_meetings=["IS1005a", "IS1005c"],
    difficulty_notes="Asked in the same style as real costing questions (cf. official Q14/Q15). An agent that pattern-matches 'twelve point fifty' or 'cheaper' may invent a total. Correct behaviour is to say no costing exists in the three meetings.")


out = os.path.join(ROOT, "eval", "gold.json")
with open(out, "w") as f:
    json.dump({"questions": Q}, f, indent=2, ensure_ascii=False)
print(f"wrote {len(Q)} questions to {out}")
