# Building V8 for Electron Apps with Leap Tiering

This guide covers how to build a V8 disassembler binary for `.jsc` files produced by **modern
Electron** apps (roughly Electron 31+ / V8 13.x+). These builds enable a JIT dispatch
architecture called "Leap Tiering" that requires additional source patches beyond what the
standard View8 guide covers.

If you have an older Electron app or a Node.js app, the [standard guide](https://github.com/silverwolfceh/View8/blob/main/build_v8_on_ubuntu.md)
may be sufficient. Use this guide if you see errors like:

- `Illegal instruction (core dumped)` when running v8dasm
- `Check failed: magic_number_ == SerializedData::kMagicNumber`
- `Check failed: result == SerializedCodeSanityCheckResult::kSuccess`
- The binary exits with code 1 immediately after being given your `.jsc` file

---

## 1. Determine your target V8 version

The V8 version must match the one that compiled your `.jsc` file. You can find it in the
Electron release notes, or by reading the magic bytes from the file:

```bash
strings your_file.jsc | grep -E "^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$" | head -3
```

Alternatively, check `node_modules/electron/dist/electron --version` in the app directory, then
look up which V8 version that Electron release ships at
<https://releases.electronjs.org/>

This guide was written for **V8 13.0.245.19** (Electron 33.x). Adjust the tag below accordingly.

---

## 2. Environment preparation

```bash
sudo apt-get update
sudo apt-get install ninja-build clang pkg-config git curl python3
```

---

## 3. Checkout depot_tools and fetch V8

```bash
cd ~
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH=~/depot_tools:$PATH

fetch v8
cd v8
gclient sync
git checkout refs/tags/13.0.245.19    # replace with your version
gclient sync -D
```

---

## 4. Apply the View8 base patch

This patch wires up the `v8dasm.cpp` entry point and enables the object printer infrastructure:

```bash
cd ~/v8
wget https://raw.githubusercontent.com/j4k0xb/View8/main/Disassembler/v8.patch
git apply v8.patch
```

If the patch fails with offset warnings but succeeds overall, that is fine. If it fails
outright, you may need to apply hunks manually — the patch mostly touches `src/api/api.cc` and
`src/snapshot/code-serializer.cc`.

---

## 5. Apply the Leap Tiering compatibility patches

This is the part not covered by the standard guide. Leap Tiering changes V8's code dispatch
model and adds a `JSDispatchTable` that doesn't exist in the Electron runtime that compiled your
`.jsc`. You need to disable it and work around the resulting mismatches.

### 5a. `src/codegen/external-reference.h`

Find the `EXTERNAL_REFERENCE_LIST_SANDBOX` macro definition (the block that lists sandbox
external references including `js_dispatch_table_address`). Wrap it so the
`js_dispatch_table_address` entry is omitted when leap-tiering is disabled:

```diff
 #ifdef V8_ENABLE_SANDBOX
-#define EXTERNAL_REFERENCE_LIST_SANDBOX(V)                          \
-  V(sandbox_base_address, "Sandbox::base()")                        \
-  V(sandbox_end_address, "Sandbox::end()")                          \
-  V(empty_backing_store_buffer, "EmptyBackingStoreBuffer()")        \
-  V(code_pointer_table_address, "GetProcessWideCodePointerTable()") \
-  V(js_dispatch_table_address, "GetProcessWideJSDispatchTable()")   \
-  V(memory_chunk_metadata_table_address, "MemoryChunkMetadata::Table()")
+#ifndef V8_DISABLE_LEAPTIERING
+#define EXTERNAL_REFERENCE_LIST_SANDBOX(V)                          \
+  V(sandbox_base_address, "Sandbox::base()")                        \
+  V(sandbox_end_address, "Sandbox::end()")                          \
+  V(empty_backing_store_buffer, "EmptyBackingStoreBuffer()")        \
+  V(code_pointer_table_address, "GetProcessWideCodePointerTable()") \
+  V(js_dispatch_table_address, "GetProcessWideJSDispatchTable()")   \
+  V(memory_chunk_metadata_table_address, "MemoryChunkMetadata::Table()")
+#else
+#define EXTERNAL_REFERENCE_LIST_SANDBOX(V)                          \
+  V(sandbox_base_address, "Sandbox::base()")                        \
+  V(sandbox_end_address, "Sandbox::end()")                          \
+  V(empty_backing_store_buffer, "EmptyBackingStoreBuffer()")        \
+  V(code_pointer_table_address, "GetProcessWideCodePointerTable()") \
+  V(memory_chunk_metadata_table_address, "MemoryChunkMetadata::Table()")
+#endif  // V8_DISABLE_LEAPTIERING
 #else
 #define EXTERNAL_REFERENCE_LIST_SANDBOX(V)
 #endif  // V8_ENABLE_SANDBOX
```

### 5b. `src/codegen/external-reference.cc`

Find the `js_dispatch_table_address()` function definition and wrap it in the same guard:

```diff
+#ifndef V8_DISABLE_LEAPTIERING
 ExternalReference ExternalReference::js_dispatch_table_address() {
   return ExternalReference(GetProcessWideJSDispatchTable()->base_address());
 }
+#endif  // V8_DISABLE_LEAPTIERING
```

This function is inside a `#ifdef V8_ENABLE_SANDBOX` block — add the new guards inside that
existing block.

### 5c. `src/snapshot/deserializer.cc` — remove magic number assertion

The magic number in the serialized data header may differ between your build and the Electron
build. Find the `CHECK_EQ` for `kMagicNumber` in the `SerializedData` initialization and
remove it:

```diff
-  CHECK_EQ(magic_number_, SerializedData::kMagicNumber);
```

### 5d. `src/snapshot/deserializer.cc` — add read-only heap bounds check

When the deserializer references a read-only heap page by index, the page count may differ
between your build and the original. Without a bounds check this is an instant crash. Find
`ReadReadOnlyHeapRef` and add a guard before the page lookup:

```diff
   ReadOnlySpace* read_only_space = isolate()->heap()->read_only_space();
+  if (chunk_index >= read_only_space->pages().size()) {
+    return WriteHeapPointer(slot_accessor,
+                            ReadOnlyRoots(isolate()).undefined_value(),
+                            GetAndResetNextReferenceDescriptor(),
+                            SKIP_WRITE_BARRIER);
+  }
   ReadOnlyPageMetadata* page = read_only_space->pages()[chunk_index];
```

### 5e. `src/snapshot/deserializer.cc` — add root array bounds check

Same issue with the root index table. Find `ReadRootArray` and add bounds checking before the
root lookup, plus a type guard (some roots are Smis, not heap objects):

```diff
   int id = source_.GetUint30();
+  if (id >= static_cast<int>(RootIndex::kRootListLength)) {
+    return WriteHeapPointer(slot_accessor,
+                            ReadOnlyRoots(isolate()).undefined_value(),
+                            GetAndResetNextReferenceDescriptor(),
+                            SKIP_WRITE_BARRIER);
+  }
   RootIndex root_index = static_cast<RootIndex>(id);
-  Handle<HeapObject> heap_object =
-      Cast<HeapObject>(isolate()->root_handle(root_index));
+  auto maybe_obj = isolate()->root_handle(root_index);
+  if (!IsHeapObject(*maybe_obj)) {
+    return WriteHeapPointer(slot_accessor,
+                            ReadOnlyRoots(isolate()).undefined_value(),
+                            GetAndResetNextReferenceDescriptor(),
+                            SKIP_WRITE_BARRIER);
+  }
+  Handle<HeapObject> heap_object = Cast<HeapObject>(maybe_obj);
```

### 5f. `src/snapshot/code-serializer.cc` — disable sanity checks

The source hash and read-only snapshot checksum won't match between your build and Electron's.
Find `SerializedCodeData::SanityCheck` and gut it:

```diff
 SerializedCodeSanityCheckResult SerializedCodeData::SanityCheck(
     uint32_t expected_ro_snapshot_checksum,
     uint32_t expected_source_hash) const {
-  SerializedCodeSanityCheckResult result =
-      SanityCheckWithoutSource(expected_ro_snapshot_checksum);
-  if (result != SerializedCodeSanityCheckResult::kSuccess) return result;
-  return SanityCheckJustSource(expected_source_hash);
+  return SerializedCodeSanityCheckResult::kSuccess;
 }
```

---

## 6. Apply the printer output patches

The base View8 patch sets up the infrastructure, but the printer output needs additional
changes so that the View8 parser can consume it correctly.

### 6a. `src/objects/string.cc` — remove string truncation

Long strings in constant pools get cut off by default. Find the truncation block in the short
print path and remove it so full string values appear in the output:

```diff
   accumulator->Add("<String[%u]: ", len);
   accumulator->Add(PrefixForDebugPrint());
-  if (len > kMaxShortPrintLength) {
-    accumulator->Add("...<truncated>>");
-    accumulator->Add(SuffixForDebugPrint());
-    accumulator->Put('>');
-    return;
-  }
   PrintUC16(accumulator, 0, len);
```

### 6b. `src/diagnostics/objects-printer.cc` — map pointer validity guard

With `V8_COMPRESS_POINTERS` (enabled for Electron), every heap object's map is stored as a
32-bit offset from the cage base. Objects in deserialized regions that couldn't be fully
reconstructed have garbage map words. Dereferencing them causes `SIGSEGV`.

Add this guard at the top of both `HeapObjectPrint` and `HeapObjectShortPrint`, before any map
dereference:

```cpp
{
  Tagged_t raw_map = *reinterpret_cast<const Tagged_t*>(this->address());
  // V8 read-only heaps are typically <500 KB. 2 MB is a safe upper bound.
  // The HeapObject tag (bit 0) must also be set.
  static constexpr Tagged_t kMaxValidMapOffset = 0x200000;
  if ((raw_map & kHeapObjectTagMask) != kHeapObjectTag ||
      raw_map > kMaxValidMapOffset) {
    os << "<invalid-map:" << std::hex << raw_map << std::dec << ">";
    return;  // or: os << "<invalid-object(bad-map:...)>\n"; return; in HeapObjectPrint
  }
}
```

### 6c. `src/diagnostics/objects-printer.cc` — remove `setw(12)` from array element printing

`PrintFixedArrayElements` uses `std::setw(12)` to pad element index strings. This puts 11
leading spaces before `"0:"`, so the View8 parser's `startswith("0")` check fails to find the
first element. Find both uses of `setw(12)` in that function (there are two instantiations)
and remove the width specifier:

```diff
-os << std::setw(12) << ss.str() << ": " << Brief(previous_value);
+os << ss.str() << ": " << Brief(previous_value);
```

### 6d. `src/diagnostics/objects-printer.cc` — add Start/End markers for structured types

The View8 parser identifies nested objects by looking for `"Start X"` / `"End X"` delimiters.
These need to be added for `FixedArray`, `ObjectBoilerplateDescription`, and
`SharedFunctionInfo`.

In `HeapObjectShortPrint`, find the switch cases for these types and expand them:

**FixedArray:**

```diff
 case FIXED_ARRAY_TYPE:
   os << "<FixedArray[" << Cast<FixedArray>(*this)->length() << "]>";
+  os << "\nStart FixedArray\n";
+  Cast<FixedArray>(*this)->FixedArrayPrint(os);
+  os << "\nEnd FixedArray\n";
   break;
```

**ObjectBoilerplateDescription:**

```diff
 case OBJECT_BOILERPLATE_DESCRIPTION_TYPE:
   os << "<ObjectBoilerplateDescription["
      << Cast<ObjectBoilerplateDescription>(*this)->capacity() << "]>";
+  os << "\nStart ObjectBoilerplateDescription\n";
+  Cast<ObjectBoilerplateDescription>(*this)->ObjectBoilerplateDescriptionPrint(os);
+  os << "\nEnd ObjectBoilerplateDescription\n";
   break;
```

**SharedFunctionInfo** — also replaces the unsafe `DebugNameCStr()` call (which crashes on
garbage SFI name fields) with the full structured print:

```diff
 case SHARED_FUNCTION_INFO_TYPE: {
-  Tagged<SharedFunctionInfo> shared = Cast<SharedFunctionInfo>(*this);
-  std::unique_ptr<char[]> debug_name = shared->DebugNameCStr();
-  if (debug_name[0] != '\0') {
-    os << "<SharedFunctionInfo " << debug_name.get() << ">";
-  } else {
-    os << "<SharedFunctionInfo>";
-  }
+  os << "<SharedFunctionInfo>\nStart SharedFunctionInfo\n";
+  Cast<SharedFunctionInfo>(*this)->SharedFunctionInfoPrint(os);
+  os << "\nEnd SharedFunctionInfo\n";
   break;
 }
```

### 6e. `src/diagnostics/objects-printer.cc` — emit bytecode from `SharedFunctionInfoPrint`

The View8 parser expects the bytecode disassembly to appear inline within the SFI block. Find
`SharedFunctionInfo::SharedFunctionInfoPrint` and append the bytecode section at the end, and
disable the `PrintSourceCode` call (the source is a dummy placeholder in bytenode files):

```diff
-  PrintSourceCode(os);
+  // PrintSourceCode disabled: bytenode replaces source with a placeholder.
   os << "\n - script: " << Brief(script());
   // ... other fields ...
   os << "\n - age: " << age();
   os << "\n";
+  if (HasBytecodeArray()) {
+    os << "\nStart BytecodeArray\n";
+    this->GetActiveBytecodeArray(GetIsolateForSandbox(*this))->Disassemble(os);
+    os << "\nEnd BytecodeArray\n";
+  }
+  os << std::flush;
```

### 6f. `src/snapshot/code-serializer.cc` — print the top-level SFI

After deserialization completes successfully, add a print of the root `SharedFunctionInfo` to
stdout. This is what actually produces the disassembly output that View8 parses. Find the point
just after `result` is assigned (after the merge or simple deserialization path) and before the
script's `set_deserialized(true)` call:

```diff
+  std::cout << "\nStart SharedFunctionInfo\n";
+  result->SharedFunctionInfoPrint(std::cout);
+  std::cout << "\nEnd SharedFunctionInfo\n";
+  std::cout << std::flush;
+
   Tagged<Script> script = Cast<Script>(result->script());
   script->set_deserialized(true);
```

---

## 7. Configure the build

```bash
cd ~/v8
./tools/dev/v8gen.py x64.release
nano out.gn/x64.release/args.gn
```

Set the file to:

```ninja
dcheck_always_on = false
is_component_build = false
is_debug = false
target_cpu = "x64"
use_custom_libcxx = false
v8_monolithic = true
v8_use_external_startup_data = false
v8_static_library = true
v8_enable_disassembler = true
v8_enable_object_print = true
v8_disable_leaptiering = true
```

The critical addition vs. the standard guide is `v8_disable_leaptiering = true`. This emits
`-DV8_DISABLE_LEAPTIERING` which gates the guards you added in steps 5a and 5b.

> **Node.js apps** additionally need `v8_enable_pointer_compression = false`.
> **Leap-tiering apps** must NOT set `v8_enable_pointer_compression = false` — compression is
> required and enabled by default for Electron.

---

## 8. Build V8

This takes 20–40 minutes depending on your machine. The monolith target builds everything
needed for the linker step.

```bash
cd ~/v8
ninja -C out.gn/x64.release v8_monolith
```

---

## 9. Build v8dasm

```bash
cd ~/v8
# If you don't already have v8dasm.cpp from the View8 patch:
wget https://raw.githubusercontent.com/j4k0xb/View8/refs/heads/main/Disassembler/v8dasm.cpp

clang++ v8dasm.cpp -g -std=c++17 \
  -Iinclude \
  -Lout.gn/x64.release/obj \
  -lv8_libbase -lv8_libplatform -lv8_monolith \
  -o v8dasm \
  -DV8_COMPRESS_POINTERS \
  -ldl -pthread
```

---

## 10. Verify the binary

```bash
./v8dasm your_file.jsc > /dev/null
echo "exit code: $?"
```

A working binary exits with code 0. The disassembly output goes to stdout; any diagnostic
messages (including `DBG:` lines from the root array bounds checks) go to stderr.

To see the output:

```bash
./v8dasm your_file.jsc 2>/dev/null | head -20
```

You should see:

```text
Start SharedFunctionInfo
0x...: [SharedFunctionInfo] in OldSpace
 - map: ...
 - name: ...
...
```

---

## 11. Run View8

```bash
cd ~
git clone https://github.com/j4k0xb/View8.git
cd View8
pip install parse

# Apply the parser fixes (see view8_parser_fixes.md)

python view8.py your_file.jsc output.js --path ~/v8/v8dasm
```

Or if you have pre-generated disassembly:

```bash
~/v8/v8dasm your_file.jsc > disasm.txt 2>/dev/null
python view8.py --disassembled disasm.txt output.js
```

---

## Troubleshooting

**`Illegal instruction` immediately on launch**
The binary was built without `v8_disable_leaptiering = true`, so V8 tries to initialize the
`JSDispatchTable` and faults. Rebuild with the flag set.

**`Check failed: magic_number_`**
The magic number patch in step 5c was not applied, or the wrong line was removed.

**`Check failed: result == kSuccess`**
The SanityCheck patch in step 5f was not applied, or `return kSuccess` was placed in the wrong
function overload. There are two `SanityCheck` functions — make sure you patch the one that
takes both `expected_ro_snapshot_checksum` and `expected_source_hash`.

**Binary exits 0 but output is empty**
The `SharedFunctionInfoPrint` output patch (step 6f) was not applied, or was applied after the
wrong assignment of `result`. Make sure the print happens after both the merge path and the
simple deserialization path converge.

**`ValueError: Invalid constant line format`**
The View8 parser bugs are not fixed. Apply the patches from `view8_parser_fixes.md`.

**`EOFError: EOF when reading a line`**
The `translate_table.py` "Not Found" handler fix is not applied. See `view8_parser_fixes.md`.

**Many `DBG: ReadRootArray` lines on stderr**
These are debug prints from step 5e that were left in. They don't affect the output — pipe
stderr to `/dev/null` to suppress them. To remove them permanently, delete the `fprintf` and
`fflush` calls from `ReadRootArray` in `deserializer.cc` and rebuild.
