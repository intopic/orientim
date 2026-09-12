# -*- coding: utf-8 -*-
"""Event-stream framing, and nothing else.

Framing is a transport concern. It knows about line endings, fields, comments
and blank lines, and it knows **nothing** about what a payload means: not that
it is JSON, not that a particular string ends a stream, not that a record must
parse into an object to have arrived. What comes out is text, and the text is
handed to a parser strict enough to be worth handing it to.

Two conventions this deliberately does not have, both of which live in
`orientim.model` because model providers earned them there:

    a `[DONE]` terminator      a model-API convention, not part of SSE and not
                               part of MCP. A stream ends when it ends
    JSON inside the framer     a payload that does not parse is a *parse*
                               result, not a framing result, and merging the
                               two loses which one failed

So a record that arrives and cannot be read is still a record that arrived,
and this says so. The bound below is this reader's own and is reported with
the answer, never as a property of the stream.

The framing follows the event-stream rules: CRLF, CR or LF end a line; a line
that is empty ends a record; a line beginning with `:` is a comment; a field
is `name: value` with exactly one leading space removed; `data` fields within
a record concatenate with a newline; `id` sets the last event id and persists
past the record that set it; and a record whose data comes to nothing
**dispatches nothing** while still setting the id it carried.
"""
import re

_EOL = re.compile(r"\r\n|\r|\n")

#: How many records this reader will frame. A stream longer than this is not
#: enumerated, which is a fact about this reader.
MAX_RECORDS = 200


def frames(text):
    """Text in, records out, with the losses named apart.

    Returns `{"records": [...], "losses": {...}, "last_id": str|None}`, where
    `last_id` is the last `id` **seen** rather than the last one dispatched: a
    record that carried an id and no data still sets it, and a resuming client
    would send that one back. A record is
    `{"data": str, "id": str|None, "event": str|None, "index": int}` where
    `data` is the payload **as text**: unparsed, uninterpreted, and not
    checked for being anything in particular.

    The losses, kept apart because they are different facts:

        unterminated   the stream stopped inside a record. The record never
                       ended, so it was never dispatched, and its data
                       arrived without being read
        no_data        a record that carried no `data` field, or whose data
                       fields came to the empty string. The framing rules
                       dispatch neither. An `id` in such a record still
                       counts, and a reader that turns it into an empty
                       message has invented a message nobody sent
        comments       lines beginning with `:`. A keepalive is one of these,
                       and a record made only of comments dispatches nothing
                       and is not a loss
        unread         records past MAX_RECORDS: arrived, not framed here
    """
    records = []
    losses = {"unterminated": 0, "no_data": 0, "comments": 0, "unread": 0}
    if not text:
        return {"records": records, "losses": losses, "last_id": None}

    lines = _EOL.split(text)
    # The last element of a split is what followed the final line ending, and
    # is not a line. Non-empty means the stream stopped mid-line.
    trailing = lines.pop() if lines else ""

    data, last_id, event, pending = [], None, None, False
    for line in lines:
        if line == "":
            if not pending:                 # nothing but comments, or a gap
                continue
            payload = "\n".join(data)
            if payload:
                if len(records) >= MAX_RECORDS:
                    losses["unread"] += 1
                else:
                    records.append({"data": payload, "id": last_id,
                                    "event": event, "index": len(records)})
            else:
                losses["no_data"] += 1
            data, event, pending = [], None, False
            continue
        if line.startswith(":"):
            losses["comments"] += 1
            continue
        name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        pending = True
        if name == "data":
            data.append(value)
        elif name == "id":
            # A U+0000 in an id is ignored by the framing rules.
            if "\x00" not in value:
                last_id = value
        elif name == "event":
            event = value
        # `retry` and any unknown field carry no payload and are ignored.

    if pending or trailing:
        losses["unterminated"] += 1
    return {"records": records, "losses": losses, "last_id": last_id}
