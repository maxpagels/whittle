"""After an edit to a file with a Whittle choice point, remind Claude that evaluation is available."""

import json, re, sys

NOTE = ("This file contains Whittle choice points ({points}). If this edit changes a default or an action list, "
        "tell the user that default changes can be checked with /whittle:eval before shipping, and that actions "
        "added now have no logged data yet.")


def main():
    try:
        path = json.load(sys.stdin)["tool_input"]["file_path"]
        with open(path, errors="ignore") as f:
            points = re.findall(r"""whittle\.decide\(\s*["']([^"']+)""", f.read())
    except (OSError, ValueError, KeyError, TypeError):
        return  # a hook must never get in the way of an edit
    if points:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": NOTE.format(points=", ".join(sorted(set(points))))}}))


if __name__ == "__main__":
    main()
