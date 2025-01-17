from random import choice
from typing import AbstractSet, Iterator, MutableSequence, Sequence

from pynvim_pp.text_object import is_word


def lower(text: str) -> str:
    return text.casefold()


def coalesce(
    chars: Sequence[str], unifying_chars: AbstractSet[str], include_syms: bool
) -> Iterator[str]:
    backwards = choice((True, False))

    words: MutableSequence[str] = []
    syms: MutableSequence[str] = []

    def w_it() -> Iterator[str]:
        if words:
            word = "".join(reversed(words) if backwards else words)
            words.clear()
            yield word

    def s_it() -> Iterator[str]:
        if syms:
            sym = "".join(reversed(syms) if backwards else syms)
            syms.clear()
            yield sym

    for char in reversed(chars) if backwards else chars:
        if is_word(char, unifying_chars=unifying_chars):
            words.append(char)
            yield from s_it()
        elif not char.isspace():
            if include_syms:
                syms.append(char)
            yield from w_it()
        else:
            yield from w_it()
            yield from s_it()

    yield from w_it()
    yield from s_it()
