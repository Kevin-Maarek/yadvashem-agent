import re
import unicodedata


def lexical(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = "".join(character for character in value
                    if not ("\u0591" <= character <= "\u05c7" and unicodedata.category(character) == "Mn"))
    value = value.translate(str.maketrans({"־": "-", "׳": "'", "״": '"'}))
    return " ".join(value.split())


def signature_key(value: str) -> str:
    return re.sub(r"\s+", "", lexical(value))

