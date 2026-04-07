# View8 Parser Bug Fixes

Two bugs in View8's parser prevent it from working with real-world JavaScript bundles compiled
by modern V8 versions. Both fixes are in `Parser/sfi_file_parser.py` and
`Translate/translate_table.py`.

---

## Bug 1: `parse_object` causes catastrophic stream corruption

### What breaks

Any `.jsc` file whose constant pools contain `ObjectBoilerplateDescription` objects (i.e.,
any file with object literals like `{}` or `{key: value}`) will fail with a confusing error:

```
ValueError: Invalid constant line format: End SharedFunctionInfo
```

or

```
ValueError: Invalid constant line format: Start SharedFunctionInfo
```

The error is misleading — it looks like a delimiter mismatch, but the root cause is stream
corruption that happens much earlier.

### Why it breaks

`parse_object` was implemented to call `parse_const_array`, which finds the element count by
scanning forward until it sees a `"- length:"` line:

```python
# OLD CODE — broken
def parse_object(lines, func_name):
    if "Start " not in (line := next(lines)):
        raise Exception(...)
    const_list = iter(parse_const_array(lines, func_name)[1:])   # ← problem here
    object_literal = "{" + ...
    while "End " not in (line := next(lines)):
        pass
    return object_literal
```

This works for `FixedArray` because V8 prints its header as:

```
0x...: [FixedArray] in OldSpace
 - map: ...
 - length: 4          ← parse_const_array finds this
           0: ...
```

But `ObjectBoilerplateDescription` is printed differently — it uses `- capacity:` instead of
`- length:`:

```
0x...: [ObjectBoilerplateDescription] in OldSpace
 - map: ...
 - capacity: 2        ← NOT "- length:", so parse_const_array never stops here
 - backing_store_size: 1
 - flags: 8
 - elements:
           0: 0x... <String[5]: #value>
           1: 0x... <String[6]: #Module>

End ObjectBoilerplateDescription
```

`parse_const_array` skips `"- capacity:"` and keeps reading past `"End ObjectBoilerplateDescription"`,
past subsequent constant pool elements, past nested `SharedFunctionInfo` blocks, all the way
until it eventually finds a `"- length:"` line somewhere else in the file — often a field like
`"- length: 5"` from a `SharedFunctionInfo` that represents a function's formal parameter count.
It then uses that number as the element count and attempts to parse 5 "elements" from whatever
bytecode or header lines follow. The resulting stream position is now completely wrong, causing
downstream `parse_const_line` calls to receive `"End SharedFunctionInfo"` or `"Start SharedFunctionInfo"`
as if they were constant pool entry lines.

There was also a secondary bug: `[1:]` was applied to the result, skipping element 0. This is a
vestige from an older V8 format where element 0 was apparently a header value. In the current V8
output, element 0 is the first property key, so slicing it away was also wrong.

### The fix

Rewrite `parse_object` to iterate the stream directly until `"End "`, matching element lines
with the same regex used elsewhere in the parser. Each value is dispatched inline using the same
logic as `parse_const_line`:

```python
def parse_object(lines, func_name):
    if "Start " not in (line := next(lines)):
        raise Exception(f"Error got line \"{line}\" not Start Object")
    # ObjectBoilerplateDescription has no "- length:" header, only "- capacity:",
    # so we can't use parse_const_array. Read element lines directly until "End ".
    elements = []
    while "End " not in (line := next(lines)):
        match = re.search(r"^(\d+(?:\-\d+)?):\s(0x[0-9a-fA-F]+\s)?(.+)", line)
        if not match:
            continue
        _, address, value = match.groups()
        if not address:
            elements.append(value)
        elif value == "<null>":
            elements.append("null")
        elif value.startswith("<String"):
            v = value.split("#", 1)[-1].rstrip('> ').replace('"', '\\"')
            elements.append(f'"{v}"')
        elif value.startswith("<SharedFunctionInfo"):
            sfi_name = value.split(" ", 1)[-1].rstrip('> ') if " " in value else ""
            elements.append(parse_shared_function_info(lines, sfi_name, func_name))
        elif value.startswith("<ArrayBoilerplateDescription") or value.startswith("<FixedArray"):
            elements.append(parse_array(lines, func_name))
        elif value.startswith("<ObjectBoilerplateDescription"):
            elements.append(parse_object(lines, func_name))
        elif value.startswith("<Odd Oddball"):
            elements.append("null")
        elif value.startswith("<BigInt"):
            elements.append(parse("<BigInt {}>", value)[0] + "n")
        else:
            elements.append(value.rstrip('>').split(" ", 1)[-1])
    it = iter(elements)
    object_literal = "{" + ", ".join([f"{key}: {value}" for key, value in zip(it, it)]) + "}"
    return object_literal
```

This approach is safe regardless of what header fields `ObjectBoilerplateDescription` (or any
future type routed through `parse_object`) happens to print — it only looks for element lines
matching the `INDEX: ADDRESS VALUE` pattern and stops cleanly at `"End "`.

---

## Bug 2: Unknown opcode handler crashes in non-interactive mode

### What breaks

Any bytecode that contains an opcode not listed in `translate_table.py` (for example,
`ToBoolean`, which was added in newer V8 versions) causes an `EOFError`:

```
Operator ToBoolean was not found in table
EOFError: EOF when reading a line
```

This makes View8 completely unusable in scripts, CI pipelines, or any non-terminal context.

### Why it breaks

The fallback handler in `translate_table.py` used Python's `input()` builtin to interactively
prompt the user:

```python
# OLD CODE — broken in non-interactive mode
"Not Found": lambda obj: input(f"Operator {obj.operator} was not found in table") and f"//{obj.operator})",
```

`input()` reads from stdin. When there is no terminal attached (piped output, background
process, etc.), stdin is immediately at EOF, so `input()` raises `EOFError`. The function also
had a mismatched parenthesis — `f"//{obj.operator})"` has a trailing `)` that would appear in
the output.

### The fix

Replace with a non-interactive fallback that emits a comment:

```python
"Not Found": lambda obj: f"//{obj.operator}",
```

Unknown opcodes are now silently rendered as `//OpcodeName` in the translated output, making
the rest of the function still readable. The print-to-stderr side-effect (which still says
`"Operator X was not found in table"`) was kept by the original intent — if you want to know
which opcodes are missing, redirect stderr separately.

---

## Impact

Both bugs affect any V8 version newer than the one View8 was originally written for. Bug 1
affects every `.jsc` file that contains object literals (nearly every real application).
Bug 2 affects any file using opcodes introduced after the translation table was last updated.
Together they made View8 crash before producing any output on modern Electron/Node applications.
