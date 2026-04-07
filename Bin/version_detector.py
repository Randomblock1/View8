import sys
import struct
import argparse

# Hash algorithm ported from main.c (v8/src/utils/version.h logic)
# Uses meet-in-the-middle to avoid 40M-iteration brute force.

_M    = 0xC6A4A7935BD1E995
_M32  = _M & 0xFFFFFFFF
_INV_M32 = pow(_M32, -1, 2**32)   # modular inverse of M32 mod 2^32


def _h32(v):
    v = v & 0xFFFFFFFF
    v = (~v + (v << 15)) & 0xFFFFFFFF
    v = (v ^ (v >> 12)) & 0xFFFFFFFF
    v = (v + (v << 2)) & 0xFFFFFFFF
    v = (v ^ (v >> 4)) & 0xFFFFFFFF
    v = (v * 2057) & 0xFFFFFFFF
    v = (v ^ (v >> 16)) & 0xFFFFFFFF
    return v


def _f_lo(v):
    """Lower 32 bits of the 'h' constant produced by hash_combine for component v.

    hash_combine(seed, H32(v)) = (seed ^ f) * M  mod 2^64
    where f = H32(v)*M ^ (H32(v)*M >> 47) * M

    Only the lower 32 bits of f affect the lower 32 bits of the result,
    so we store just those.
    """
    h = (_h32(v) * _M) & 0xFFFFFFFFFFFFFFFF
    h ^= h >> 47
    h = (h * _M) & 0xFFFFFFFFFFFFFFFF
    return h & 0xFFFFFFFF


def _step(seed32, f_lo):
    """Forward: lower 32 bits of hash_combine(seed32, f)."""
    return ((seed32 ^ f_lo) * _M32) & 0xFFFFFFFF


def _step_inv(result32, f_lo):
    """Inverse: recover seed32 from result32 and f_lo."""
    return ((result32 * _INV_M32) & 0xFFFFFFFF) ^ f_lo


# --- Public API ---

def calculate_version_hash(major, minor, build, patch):
    """V8 version order: major.minor.build.patch  (e.g. 13.0.245.19)."""
    s = _step(0,  _f_lo(major))
    s = _step(s,  _f_lo(minor))
    s = _step(s,  _f_lo(build))
    s = _step(s,  _f_lo(patch))
    return s


def bruteforce_version(target_hash):
    """Meet-in-the-middle: O(major*minor + build*patch) instead of O(all four)."""
    # Ranges matching the C reference implementation
    MAJOR = 20
    MINOR = 20
    BUILD = 500
    PATCH = 200

    # Precompute f_lo tables
    f_major = [_f_lo(i) for i in range(MAJOR)]
    f_minor = [_f_lo(i) for i in range(MINOR)]
    f_build = [_f_lo(i) for i in range(BUILD)]
    f_patch = [_f_lo(i) for i in range(PATCH)]

    # Forward table: s2 -> [(major, minor), ...]
    s2_table = {}
    for maj in range(MAJOR):
        s1 = _step(0, f_major[maj])
        for mn in range(MINOR):
            s2 = _step(s1, f_minor[mn])
            if s2 not in s2_table:
                s2_table[s2] = []
            s2_table[s2].append((maj, mn))

    # Invert the last two steps for each (patch, build) and look up s2
    for pat in range(PATCH):
        s3 = _step_inv(target_hash, f_patch[pat])
        for bld in range(BUILD):
            s2 = _step_inv(s3, f_build[bld])
            if s2 in s2_table:
                for maj, mn in s2_table[s2]:
                    # Verify to rule out 32-bit hash collisions
                    if calculate_version_hash(maj, mn, bld, pat) == target_hash:
                        return f"{maj}.{mn}.{bld}.{pat}"
    return None


def main():
    parser = argparse.ArgumentParser(description="Recreation of VersionDetector.exe", add_help=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-h", "--hash-ver", help="Hash a version string (e.g., 13.0.245.19)")
    group.add_argument("-d", "--decode-hash", help="Decode a hex hash value")
    group.add_argument("-f", "--file", help="Decode a hash from a file at offset 4")
    parser.add_argument("--help", action="help", help="show this help message and exit")

    args = parser.parse_args()

    if args.hash_ver:
        try:
            parts = [int(p) for p in args.hash_ver.split('.')]
            if len(parts) != 4:
                raise ValueError
            h = calculate_version_hash(*parts)
            print(f"Hash: {h:08x}")
        except ValueError:
            print("Invalid version format. Expected format: major.minor.build.patch", file=sys.stderr)
            sys.exit(1)

    elif args.decode_hash:
        try:
            target = int(args.decode_hash, 16) & 0xFFFFFFFF
            result = bruteforce_version(target)
            if result:
                print(result)
            else:
                print(f"Error: can't find version for hash 0x{target:08x}", file=sys.stderr)
                sys.exit(1)
        except ValueError:
            print("Invalid hash format. Expected hex value.", file=sys.stderr)
            sys.exit(1)

    elif args.file:
        try:
            with open(args.file, "rb") as f:
                f.seek(4)
                data = f.read(4)
                if len(data) < 4:
                    print("Error reading from file: file too short", file=sys.stderr)
                    sys.exit(1)
                target = struct.unpack("<I", data)[0]
                print(f"Detecting version for hash 0x{target:08x}...", file=sys.stderr)
                result = bruteforce_version(target)
                if result:
                    print(result)
                else:
                    print(f"Error: can't find version for hash 0x{target:08x} within current ranges.", file=sys.stderr)
                    sys.exit(1)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
