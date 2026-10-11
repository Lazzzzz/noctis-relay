"""Offline FR/EN filter for insults and slurs in echo chat lines.

Self-contained (standard library only, no network, no model): the word lists
below were written for this project and are published under the relay's MIT
licence. The approach follows common open-source filters such as
better_profanity (MIT, custom lists and leetspeak variants) and wiqaya (MIT,
FR/EN lists); no code or list was copied from them.

Matching works on whole tokens after normalisation, so innocent words that
merely contain a bad sequence (passer, assassin, classique, Scunthorpe) are
kept. Normalisation folds case and accents (NFKD), common look-alike letters
(Cyrillic/Greek), simple leetspeak (0→o, 1→i, 3→e, 4→a, 5→s, 7→t, @→a, $→s),
deliberately stretched letters (fuuuck), single letters spaced or dotted out
(f.u.c.k) and masked letters (f*ck). It is not a guarantee: context, new
spellings, other languages and creative evasion still get through, and a
word that is offensive only in context is not caught. Lines that match are
dropped whole; nothing is rewritten.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Exact tokens (after normalisation).
_EXACT_EN = """
shit shits shitty shithead shitheads bullshit bitch bitches bitchy asshole assholes arsehole
dumbass jackass dickhead dickheads cocksucker cocksuckers twat twats wanker wankers whore
whores slut sluts bastard bastards cunt cunts retarded tranny trannies
faggot faggots spic spics kike kikes nigger niggers
nigga niggas kys motherfucker motherfuckers douchebag pussy
"""
_EXACT_FR = """
con conne connes connard connards connasse connasses salope salopes salaud salauds
pute putes putain putains petasse petasses batard batards fdp ntm tg encule encules enculee
enculer enculeur pd pede pedes gouine gouines tarlouze tarlouzes tafiole tafioles bougnoule
bougnoules youpin youpins merde merdes merdique merdeux couille couilles couillon couillons
branleur branleurs branleuse abruti abrutis abrutie cretin cretins cretine imbecile imbeciles
debile debiles niquer nique niquez niqué negro negros bicot bicots
"""
# Prefixes that are unambiguous at the start of a token (fucking, enculés, niquée...).
_PREFIXES = (
    "fuck", "motherfuck", "encul", "niqu", "connard", "connass", "salop", "bitch", "cunt",
    "whor", "slut", "wank", "fagg", "nigg", "merdi", "merdeu", "couill", "branl", "batard",
    "putain", "shitt", "asshol", "cocksuck", "tarlouz", "bougnoul",
)
# Whole phrases (token sequences).
_PHRASES = (
    "fils de pute", "nique ta mere", "ntm", "ta gueule", "ferme ta gueule", "trou du cul",
    "va te faire foutre", "suicide toi", "va crever", "kill yourself", "go die", "son of a bitch",
    "sale noir", "sale arabe", "sale juif", "sale pd",
)
# Never blocked, even when a prefix matches (niggle, retardant...).
_ALLOW = """
niggle niggles niggling niggled scunthorpe passer assassin
classique cocktail analyse consonne contre concours conte conseil connu connaitre connexion
connecter cone cones bitchin
"""

_CONFUSABLES = str.maketrans({
    # Cyrillic and Greek letters that look like Latin ones.
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j",
    "к": "k", "м": "m", "н": "h", "т": "t", "в": "b", "ѕ": "s", "ԁ": "d", "ɡ": "g",
    "α": "a", "ο": "o", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "τ": "t", "υ": "u", "ρ": "p",
    "ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d",
})
_LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "€": "e",
    "!": "i", "|": "i",
})
_MASK = "*#"
_TOKEN_RE = re.compile(r"[a-z*#]+")
_RUN3 = re.compile(r"(.)\1{2,}")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.translate(_CONFUSABLES)


def _words(block: str) -> set[str]:
    return {_fold(w) for w in block.split()}


@dataclass
class Filter:
    exact: set[str] = field(default_factory=lambda: _words(_EXACT_EN) | _words(_EXACT_FR))
    prefixes: tuple = _PREFIXES
    phrases: set = field(default_factory=lambda: {tuple(_fold(p).split()) for p in _PHRASES})
    allow: set[str] = field(default_factory=lambda: _words(_ALLOW))

    @classmethod
    def with_files(cls, block_text: str = "", allow_text: str = "") -> "Filter":
        """Built-in lists plus operator lists: one term per line, ``word*`` for a prefix,
        several words for a phrase, ``#`` comments."""
        f = cls()
        prefixes = list(f.prefixes)
        for line in block_text.splitlines():
            term = _fold(line.split("#", 1)[0].strip())
            if not term:
                continue
            if " " in term:
                f.phrases.add(tuple(term.split()))
            elif term.endswith("*") and len(term) > 3:
                prefixes.append(term[:-1])
            else:
                f.exact.add(term)
        f.prefixes = tuple(prefixes)
        for line in allow_text.splitlines():
            term = _fold(line.split("#", 1)[0].strip())
            if term:
                f.allow.add(term)
        return f

    def tokens(self, text: str) -> list[str]:
        folded = _fold(text)
        # Leetspeak only inside words: digits standing alone (numbers) stay numbers.
        raw = re.findall(r"[\w*#@$€!|]+", folded)
        out: list[str] = []
        letters: list[str] = []
        for word in raw:
            mapped = word.translate(_LEET) if re.search(r"[a-z]", word) else word
            parts = _TOKEN_RE.findall(mapped.replace("_", " "))
            for part in parts:
                if len(part) == 1:
                    letters.append(part)
                    continue
                if len(letters) >= 3:
                    out.append("".join(letters))
                out.extend(letters if len(letters) < 3 else ())
                letters = []
                out.append(part)
        if len(letters) >= 3:
            out.append("".join(letters))
        else:
            out.extend(letters)
        return out

    def _bad_word(self, token: str) -> bool:
        variants = {token, _RUN3.sub(r"\1\1", token), _RUN3.sub(r"\1", token)}
        for v in variants:
            if v in self.allow:
                return False
        for v in variants:
            if v in self.exact or any(v.startswith(p) for p in self.prefixes):
                return True
            if any(c in _MASK for c in v) and self._masked(v):
                return True
        return False

    def _masked(self, token: str) -> bool:
        """f*ck, s#it: masked letters stand for any letter; needs two real letters and the first."""
        if token[0] in _MASK or sum(c not in _MASK for c in token) < 2:
            return False
        def fits(part: str, word: str) -> bool:
            return all(t in _MASK or t == w for t, w in zip(part, word))

        for word in self.exact:
            if len(word) == len(token) and fits(token, word):
                return True
        return any(len(token) >= len(p) and fits(token[:len(p)], p) for p in self.prefixes)

    def blocked(self, text: str) -> bool:
        tokens = self.tokens(text)
        if any(self._bad_word(t) for t in tokens):
            return True
        for phrase in self.phrases:
            n = len(phrase)
            if n == 1:
                continue
            for i in range(len(tokens) - n + 1):
                if tuple(tokens[i:i + n]) == phrase:
                    return True
        return False


DEFAULT = Filter()
