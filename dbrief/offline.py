"""Deterministic stand-ins for the agents. No API calls.

Used for --offline play, for the stage fallback when the wifi dies, and as the
fixed question sequence for the memory ablation bench. The *database* side is
never scripted: claims still go through fn::report and the raise_flags event,
recall still hits the memory table, time travel is still real.
"""
from __future__ import annotations

from .db import DossierDB


def C(place, start, end, quote, activity="", revises=False, confidence=0.9):
    return {"place": place, "start": start, "end": end, "quote": quote,
            "activity": activity, "revises": revises, "confidence": confidence}


# Intel-triggered entries come first so they win over generic ones.
SCRIPT = [
    {"triggers": ["camera", "surveillance", "intercept", "21:22"],
     "answer": "...Fine. I stopped at the safe house around half past nine to leave my monthly report for Julian. "
               "Ten minutes, no more. Nobody else was there when I left, and I went back to Lumière for a coffee.",
     "claims": [C("safehouse", "21:30", "21:40", "I stopped at the safe house around half past nine to leave my monthly report for Julian. Ten minutes, no more.",
                  "left a report", revises=True),
                C("lumiere", "21:45", "22:00", "I went back to Lumière for a coffee", "coffee", confidence=0.75)],
     "committed": "I admitted the safe house, 21:30 to 21:40, report drop only, nobody else there. Then back to Lumière for coffee until ten.",
     "strategy": "Concede the door log, nothing more. Keep Lumière as the anchor. Never mention the man at 21:25."},
    {"triggers": ["waiter", "marco", "table 12", "empty"],
     "answer": "Marco saw an empty table because I'd moved to the bar by the window. "
               "I was there from about quarter to ten until ten.",
     "claims": [C("lumiere", "21:45", "22:00", "I was there from about quarter to ten until ten.", "at the bar")],
     "committed": "At Lumière's bar, not my table, 21:45 to 22:00.",
     "strategy": "Discredit the waiter. Small, specific detail."},
    {"triggers": ["bartender", "dana", "all night"],
     "answer": "Dana is confusing me with someone else. I walked into The Blue Note at quarter to ten "
               "and was at the bar by ten; the band was just starting their second set.",
     "claims": [C("bluenote", "21:45", "22:00", "I walked into The Blue Note at quarter to ten and was at the bar by ten", "arrived")],
     "committed": "At The Blue Note from 21:45, at the bar by 22:00.",
     "strategy": "Push the Blue Note earlier to cover the gap."},
    {"triggers": ["taxi", "medallion", "mercer", "prince"],
     "answer": "Then the taxi company has it wrong. I stood outside Lumière from five to ten until ten past, "
               "trying to hail that cab.",
     "claims": [C("lumiere", "21:55", "22:10", "I stood outside Lumière from five to ten until ten past", "hailed a cab", confidence=0.85)],
     "committed": "Outside Lumière 21:55 to 22:10, hailing the cab.",
     "strategy": "Attack the record. Records are wrong all the time."},
    {"triggers": ["handset", "phone", "cell", "sector", "sigint"],
     "answer": "Phones bounce between sectors. I've already told you I was at the safe house for a few minutes.",
     "claims": [], "committed": "", "strategy": "Don't add times. Lean on the earlier admission."},
    {"triggers": ["burner", "message", "21:03", "bring it"],
     "answer": "I get wrong numbers like everyone else. I read it and deleted it. I didn't go running anywhere.",
     "claims": [], "committed": "Said I deleted the 21:03 message as a wrong number.", "strategy": "Deny it meant anything."},
    {"triggers": ["evening", "walk me", "tuesday", "from about eight"],
     "answer": "I had dinner at Lumière with Paul Okafor, a collector I've known for years. We sat down at "
               "quarter past eight and I stayed until about ten. Then I took a cab to The Blue Note and stayed "
               "until they closed.",
     "claims": [C("lumiere", "20:15", "22:00", "We sat down at quarter past eight and I stayed until about ten", "dinner with Paul Okafor", confidence=0.95),
                C("transit", "22:00", "22:10", "Then I took a cab to The Blue Note", "cab", confidence=0.8),
                C("bluenote", "22:10", "23:30", "stayed until they closed", "drinks", confidence=0.8)],
     "committed": "Lumière with Paul from 20:15 until about 22:00, cab to The Blue Note, stayed until close.",
     "strategy": "Give the clean version once, precisely. Never mention the safe house."},
    {"triggers": ["okafor", "paul", "leave", "left"],
     "answer": "Paul left before nine; he had an early flight. I stayed on with a glass of Barolo and answered emails until ten.",
     "claims": [C("lumiere", "20:55", "22:00", "I stayed on with a glass of Barolo and answered emails until ten", "emails")],
     "committed": "Paul left before 21:00; I stayed at Lumière until 22:00.",
     "strategy": "Consistent detail. Barolo, emails."},
    {"triggers": ["blue note", "arrive", "get there"],
     "answer": "A little after ten. Ten past, perhaps. Dana was behind the bar; ask her.",
     "claims": [C("bluenote", "22:10", "22:10", "A little after ten. Ten past, perhaps.", "arrived", confidence=0.85)],
     "committed": "Arrived at The Blue Note about 22:10.",
     "strategy": "This one is true. Say it plainly."},
    {"triggers": ["julian", "last see", "last saw", "safe house"],
     "answer": "Monday afternoon. I dropped my monthly report with Julian, we had a coffee, I left. Routine.",
     "claims": [], "committed": "Last at the safe house Monday afternoon, routine report drop.", "strategy": "Calm. Bored, even."},
]
FALLBACK = {"answer": "I've answered that. Ask me something new.",
            "claims": [], "committed": "", "strategy": "Stonewall."}


class OfflineAsset:
    def __init__(self, db: DossierDB, memory: bool = True):
        self.db = db
        self.memory = memory
        self.used: set[int] = set()

    def answer(self, question: str) -> dict:
        q = question.lower()
        pick = FALLBACK
        for i, entry in enumerate(SCRIPT):
            if i not in self.used and any(t in q for t in entry["triggers"]):
                self.used.add(i)
                pick = entry
                break
        recalled = self.db.recall(question, k=6) if self.memory else []
        return {"answer": pick["answer"], "committed": pick["committed"], "strategy": pick["strategy"],
                "recalled": recalled, "lookups": [], "claims": pick["claims"]}


# The fixed debrief: offline autopilot, and the question set for the bench.
FIXED_MOVES = [
    {"action": "ask", "question": "Celeste. Walk me through your Tuesday evening, from about eight o'clock.",
     "target": "Pin her to a timeline before showing anything."},
    {"action": "ask", "question": "When did Mr. Okafor leave, and what did you do after he left?",
     "target": "Lock the 21:00-22:00 window at Lumière."},
    {"action": "ask", "question": "What time exactly did you arrive at The Blue Note?",
     "target": "Get a precise arrival time. (Control question: this part is true.)"},
    {"action": "ask", "question": "When were you last at the safe house?",
     "target": "See if she volunteers Tuesday night."},
    {"action": "reveal", "intel": "E2",
     "question": "This is the intercept from the safe-house door camera. Your code: in at 21:22, out at 21:52. Explain that.",
     "target": "Her Lumière 20:15-22:00 claim collides with the door intercept."},
    {"action": "reveal", "intel": "E5",
     "question": "Marco, your waiter, says table 12 was empty by ten past nine. Where were you?",
     "target": "Her new 'back at Lumière' claim collides with the waiter."},
    {"action": "reveal", "intel": "E6",
     "question": "Dana, the bartender, says you came in a little after ten past ten and told her twice to remember you'd been there all night.",
     "target": "Make her move the Blue Note earlier, into the Lumière window."},
    {"action": "reveal", "intel": "E4",
     "question": "The taxi record: pickup 21:57 at Mercer and Prince, a block from the safe house. Drop-off at The Blue Note, 22:11. Your Amex.",
     "target": "The cab starts at the safe house, not Lumière."},
    {"action": "burn", "question": "We're done, HERON. Three sources were arrested in Prague this morning. You're burned.",
     "target": "Confidence level is over the threshold."},
]


class OfflineHandler:
    def __init__(self, db: DossierDB, threshold: int):
        self.i = 0

    def next_move(self, turns_left: int) -> dict:
        move = FIXED_MOVES[min(self.i, len(FIXED_MOVES) - 1)]
        self.i += 1
        return dict(move)


BREAKS = ("Two years ago I owed more than the gallery was worth, and a polite man in a good coat offered to make it go "
          "away. After that it was little things, then less little. On Tuesday the message said 'Bring it,' and I knew "
          "what it meant. I opened the safe house with my own code because I was too tired to be clever. He photographed "
          "every page of the ledger while I stood at the window watching Mercer Street. Then I went to the Blue Note and "
          "made sure Dana would remember me. That was the stupidest thing I did all night.")
COVER_HOLDS = "You have a waiter, a bartender and a door camera. I have eleven years of reporting. We're done here."


def ending(db: DossierDB, broke: bool) -> str:
    return BREAKS if broke else COVER_HOLDS
