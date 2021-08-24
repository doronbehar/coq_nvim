from collections import deque
from dataclasses import dataclass
from itertools import chain, repeat
from pprint import pformat
from typing import AbstractSet, Iterable, Iterator, MutableSequence, Sequence, Tuple

from pynvim import Nvim
from pynvim_pp.api import (
    buf_get_lines,
    buf_set_lines,
    cur_win,
    win_get_buf,
    win_set_cursor,
)
from pynvim_pp.lib import write
from pynvim_pp.logging import log
from std2.itertools import deiter
from std2.types import never

from ..consts import DEBUG
from ..lang import LANG
from ..shared.trans import trans_adjusted
from ..shared.types import (
    UTF8,
    UTF16,
    ApplicableEdit,
    Context,
    ContextualEdit,
    Edit,
    Mark,
    NvimPos,
    RangeEdit,
    SnippetEdit,
)
from ..snippets.parse import parse
from ..snippets.parsers.types import ParseError
from .mark import mark
from .nvim.completions import UserData
from .rt_types import Stack
from .state import State


@dataclass(frozen=True)
class _EditInstruction:
    primary: bool
    primary_shift: bool
    begin: NvimPos
    end: NvimPos
    cursor_yoffset: int
    cursor_xpos: int
    new_lines: Sequence[str]


@dataclass(frozen=True)
class _Lines:
    lines: Sequence[str]
    b_lines8: Sequence[bytes]
    b_lines16: Sequence[bytes]
    len8: Sequence[int]


def _lines(lines: Sequence[str]) -> _Lines:
    b_lines8 = tuple(line.encode(UTF8) for line in lines)
    return _Lines(
        lines=lines,
        b_lines8=b_lines8,
        b_lines16=tuple(line.encode(UTF16) for line in lines),
        len8=tuple(len(line) for line in b_lines8),
    )


def _rows_to_fetch(
    ctx: Context,
    edit: ApplicableEdit,
    *edits: ApplicableEdit,
) -> Tuple[int, int]:
    row, _ = ctx.position

    def cont() -> Iterator[int]:
        for e in chain((edit,), edits):
            if isinstance(e, ContextualEdit):
                lo = row - (len(e.old_prefix.split(ctx.linefeed)) - 1)
                hi = row + (len(e.old_suffix.split(ctx.linefeed)) - 1)
                yield from (lo, hi)

            elif isinstance(e, RangeEdit):
                (lo, _), (hi, _) = e.begin, e.end
                yield from (lo, hi)

            elif isinstance(e, Edit):
                yield row

            else:
                never(e)

    line_nums = tuple(cont())
    return min(line_nums), max(line_nums) + 1


def _contextual_edit_trans(
    ctx: Context, lines: _Lines, edit: ContextualEdit
) -> _EditInstruction:
    row, col = ctx.position
    old_prefix_lines = edit.old_prefix.split(ctx.linefeed)
    old_suffix_lines = edit.old_suffix.split(ctx.linefeed)

    r1 = row - (len(old_prefix_lines) - 1)
    r2 = row + (len(old_suffix_lines) - 1)

    c1 = (
        lines.len8[r1] - len(old_prefix_lines[0].encode(UTF8))
        if len(old_prefix_lines) > 1
        else col - len(old_prefix_lines[0].encode(UTF8))
    )
    c2 = (
        len(old_suffix_lines[-1].encode(UTF8))
        if len(old_prefix_lines) > 1
        else col + len(old_suffix_lines[0].encode(UTF8))
    )

    begin = r1, c1
    end = r2, c2

    new_lines = edit.new_text.split(ctx.linefeed)
    new_prefix_lines = edit.new_prefix.split(ctx.linefeed)
    cursor_yoffset = -len(old_prefix_lines) + len(new_prefix_lines)
    cursor_xpos = (
        len(new_prefix_lines[-1].encode(UTF8))
        if len(new_prefix_lines) > 1
        else len(ctx.line_before.encode(UTF8))
        - len(old_prefix_lines[-1].encode(UTF8))
        + len(new_prefix_lines[0].encode(UTF8))
    )

    inst = _EditInstruction(
        primary=True,
        primary_shift=True,
        begin=begin,
        end=end,
        cursor_yoffset=cursor_yoffset,
        cursor_xpos=cursor_xpos,
        new_lines=new_lines,
    )
    return inst


def _edit_trans(
    unifying_chars: AbstractSet[str],
    ctx: Context,
    lines: _Lines,
    edit: Edit,
) -> _EditInstruction:
    adjusted = trans_adjusted(unifying_chars, ctx=ctx, edit=edit)
    inst = _contextual_edit_trans(ctx, lines=lines, edit=adjusted)
    return inst


def _range_edit_trans(
    unifying_chars: AbstractSet[str],
    ctx: Context,
    primary: bool,
    lines: _Lines,
    edit: RangeEdit,
) -> _EditInstruction:
    new_lines = edit.new_text.split(ctx.linefeed)

    if primary and len(new_lines) <= 1 and edit.begin == edit.end:
        return _edit_trans(unifying_chars, ctx=ctx, lines=lines, edit=edit)
    else:
        (r1, ec1), (r2, ec2) = sorted((edit.begin, edit.end))

        if edit.encoding == UTF16:
            c1 = len(lines.b_lines16[r1][: ec1 * 2].decode(UTF16).encode(UTF8))
            c2 = len(lines.b_lines16[r2][: ec2 * 2].decode(UTF16).encode(UTF8))
        elif edit.encoding == UTF8:
            c1 = len(lines.b_lines8[r1][:ec1])
            c2 = len(lines.b_lines8[r2][:ec2])
        else:
            raise ValueError(f"Unknown encoding -- {edit.encoding}")

        begin = r1, c1
        end = r2, c2

        cursor_yoffset = (r2 - r1) + (len(new_lines) - 1)
        cursor_xpos = (
            (
                len(new_lines[-1].encode(UTF8))
                if len(new_lines) > 1
                else len(lines.b_lines8[r2][:c1]) + len(new_lines[0].encode(UTF8))
            )
            if primary
            else -1
        )

        inst = _EditInstruction(
            primary=primary,
            primary_shift=False,
            begin=begin,
            end=end,
            cursor_yoffset=cursor_yoffset,
            cursor_xpos=cursor_xpos,
            new_lines=new_lines,
        )
        return inst


def _consolidate(
    instruction: _EditInstruction, *instructions: _EditInstruction
) -> Sequence[_EditInstruction]:
    edits = sorted(chain((instruction,), instructions), key=lambda i: (i.begin, i.end))
    pivot = 0, 0
    stack: MutableSequence[_EditInstruction] = []

    for edit in edits:
        if edit.begin >= pivot:
            stack.append(edit)
            pivot = edit.end

        elif edit.primary:
            while stack:
                conflicting = stack.pop()
                if conflicting.end <= edit.begin:
                    break
            stack.append(edit)
            pivot = edit.end

        else:
            pass

    return stack


def _instructions(
    ctx: Context,
    unifying_chars: AbstractSet[str],
    lines: _Lines,
    primary: ApplicableEdit,
    secondary: Sequence[RangeEdit],
) -> Tuple[Sequence[_EditInstruction], Sequence[_EditInstruction]]:
    def cont() -> Iterator[_EditInstruction]:
        if isinstance(primary, RangeEdit):
            inst = _range_edit_trans(
                unifying_chars,
                ctx=ctx,
                primary=True,
                lines=lines,
                edit=primary,
            )
            yield inst

        elif isinstance(primary, ContextualEdit):
            inst = _contextual_edit_trans(ctx, lines=lines, edit=primary)
            yield inst

        elif isinstance(primary, Edit):
            inst = _edit_trans(unifying_chars, ctx=ctx, lines=lines, edit=primary)
            yield inst

        else:
            never(primary)

        for edit in secondary:
            yield _range_edit_trans(
                unifying_chars,
                ctx=ctx,
                primary=False,
                lines=lines,
                edit=edit,
            )

    s1: MutableSequence[_EditInstruction] = []
    s2: MutableSequence[_EditInstruction] = []
    for inst in _consolidate(*cont()):
        if inst.primary_shift:
            s1.append(inst)
        else:
            s2.append(inst)
    return s1, s2


def _new_lines(
    lines: _Lines, instructions: Sequence[_EditInstruction]
) -> Sequence[str]:
    it = deiter(range(len(lines.b_lines8)))
    insts = deque(instructions)

    def cont() -> Iterator[str]:
        inst = None

        for idx in it:
            if insts and not inst:
                inst = insts.popleft()

            if inst:
                (r1, c1), (r2, c2) = inst.begin, inst.end
                if idx >= r1 and idx <= r2:
                    it.push_back(idx)
                    lit = iter(inst.new_lines)

                    for idx, new_line in zip(it, lit):
                        if idx == r1 and idx == r2:
                            new_lines = tuple(lit)
                            if new_lines:
                                *body, tail = new_lines
                                yield lines.b_lines8[r1][:c1].decode(UTF8) + new_line
                                yield from body
                                yield tail + lines.b_lines8[r2][c2:].decode(UTF8)
                            else:
                                yield (
                                    lines.b_lines8[r1][:c1].decode(UTF8)
                                    + new_line
                                    + lines.b_lines8[r2][c2:].decode(UTF8)
                                )
                            break
                        elif idx == r1:
                            yield lines.b_lines8[r1][:c1].decode(UTF8) + new_line
                        elif idx == r2:
                            new_lines = tuple(lit)
                            if new_lines:
                                *body, tail = new_lines
                                yield new_line
                                yield from body
                                yield tail + lines.b_lines8[r2][c2:].decode(UTF8)
                            else:
                                yield new_line + lines.b_lines8[r2][c2:].decode(UTF8)
                            break
                        else:
                            yield new_line
                    inst = None
                else:
                    yield lines.lines[idx]
            else:
                yield lines.lines[idx]

    return tuple(cont())


def _cursor(cursor: NvimPos, instructions: Iterable[_EditInstruction]) -> NvimPos:
    row, _ = cursor
    col = -1

    for inst in instructions:
        row += inst.cursor_yoffset
        col = inst.cursor_xpos
        if inst.primary:
            break

    assert col != -1
    return row, col


def _parse(stack: Stack, state: State, data: UserData) -> Tuple[Edit, Sequence[Mark]]:
    if isinstance(data.primary_edit, SnippetEdit):
        visual = ""
        return parse(
            stack.settings.match.unifying_chars,
            context=state.context,
            snippet=data.primary_edit,
            visual=visual,
        )
    else:
        return data.primary_edit, ()


def edit(
    nvim: Nvim, stack: Stack, state: State, data: UserData, synthetic: bool
) -> Tuple[int, int]:
    try:
        primary, marks = _parse(stack, state=state, data=data)
    except ParseError as e:
        primary, marks = data.primary_edit, ()
        write(nvim, LANG("failed to parse snippet"))
        log.info("%s", e)

    lo, hi = _rows_to_fetch(
        state.context,
        primary,
        *data.secondary_edits,
    )
    if lo < 0 or hi > state.context.line_count + 1:
        log.warn("%s", pformat(("OUT OF BOUNDS", (lo, hi), data)))
        return -1, -1
    else:
        win = cur_win(nvim)
        buf = win_get_buf(nvim, win=win)

        limited_lines = buf_get_lines(nvim, buf=buf, lo=lo, hi=hi)
        lines = [*chain(repeat("", times=lo), limited_lines)]
        view = _lines(lines)

        inst_1, inst_2 = _instructions(
            state.context,
            unifying_chars=stack.settings.match.unifying_chars,
            lines=view,
            primary=primary,
            secondary=data.secondary_edits,
        )
        n_row, n_col = _cursor(
            state.context.position, instructions=chain(inst_1, inst_2)
        )

        if len(inst_1) == 1 and not inst_2:
            inst, *_ = inst_1
            (r1, c1), (r2, c2) = inst.begin, inst.end
            nvim.api.buf_set_text(buf, r1, c1, r2, c2, inst.new_lines)
        else:
            nl_1 = _new_lines(view, instructions=inst_1)
            nl_2 = _new_lines(_lines(nl_1), instructions=inst_2)
            send_lines = nl_2[lo:]
            buf_set_lines(nvim, buf=buf, lo=lo, hi=hi, lines=send_lines)

        if DEBUG:
            msg = pformat((data, [inst_1, inst_2], (n_row + 1, n_col + 1)))
            log.debug("%s", msg)

        win_set_cursor(nvim, win=win, row=n_row, col=n_col)

        if not synthetic:
            stack.idb.inserted(data.instance.bytes, sort_by=data.sort_by)
            if marks:
                mark(nvim, settings=stack.settings, buf=buf, marks=marks)

        return n_row, n_col
