# Samples

Hand-verification runs, each read-only on any Mac: no run here changes a disk, a volume,
the real `/etc/fstab` or any setting. Every command is run from the project folder (the
folder holding `apfs_tm_snapfree.py`) and works with the stock `python3`.

| File | What it is |
| --- | --- |
| `fstab_unconfigured` | a stand-in fstab whose one line names a folder that does not exist, so it configures nothing a sample names |
| `fstab_unmounted.template` | a stand-in fstab with one line for a throwaway folder, `<tmp>` standing for the temporary folder it is made in |
| `expected_unconfigured.txt` | what the first `status` run prints, the temporary folder written `<tmp>` |
| `expected_unmounted.txt` | what the second `status` run prints, likewise |

## Make a throwaway folder

```bash
tmp="$(mktemp -d)"
real="$(cd "$tmp" && pwd -P)"
mkdir "$real/throwaway"
```

`real` is the temporary folder with symlinks resolved, which is how the tool prints a path
(`mktemp` answers under `/var`, a symlink to `/private/var`). The samples assume the path
holds no space, which `mktemp` never gives. Every block below opens by checking that `real`
is set, so a block run in a fresh shell says what it needs instead of writing under `/`.

## A folder that is not configured

```bash
: "${real:?run the commands under Make a throwaway folder first}"
python3 apfs_tm_snapfree.py --mode status --path "$real/throwaway" --fstab samples/fstab_unconfigured > "$real/unconfigured.txt"
echo "exit $?"
sed "s|$real|<tmp>|g" "$real/unconfigured.txt" | diff - samples/expected_unconfigured.txt && echo "matches"
```

Expected: `exit 2`, then `matches`. The fstab has no line for the folder, so status prints
its first check as "not configured" and stops there. It reads only the stand-in fstab.

## A configured folder with nothing mounted on it

```bash
: "${real:?run the commands under Make a throwaway folder first}"
sed "s|<tmp>|$real|" samples/fstab_unmounted.template > "$real/fstab"
python3 apfs_tm_snapfree.py --mode status --path "$real/throwaway" --fstab "$real/fstab" > "$real/unmounted.txt"
echo "exit $?"
sed "s|$real|<tmp>|g" "$real/unmounted.txt" | diff - samples/expected_unmounted.txt && echo "matches"
```

Expected: `exit 1`, then `matches`. The fstab line is found, the `mounted` check fails (the
folder's device is its parent's), and the five checks that would read the mounted volume
print `skipped (not mounted)` instead: run against an unmounted folder they would describe
the parent volume. This is what status reports after a reboot in which the volume failed to
mount.

Both runs read the stand-in fstab and `stat` the folder and its parent, and nothing else:
neither runs `diskutil`, `tmutil`, `mdutil` or `sudo`. To see that for yourself, put a
folder of logging stand-ins first on `PATH`:

```bash
: "${real:?run the commands under Make a throwaway folder first}"
mkdir "$real/bin"
for name in diskutil tmutil mdutil sudo ditto; do
  printf '#!/bin/sh\necho "%s $*" >> "%s/calls.log"\nexit 1\n' "$name" "$real" > "$real/bin/$name"
  chmod +x "$real/bin/$name"
done
PATH="$real/bin:$PATH" python3 apfs_tm_snapfree.py --mode status --path "$real/throwaway" --fstab "$real/fstab"
ls "$real/calls.log"
```

Expected: the same eight lines, then `ls` saying `calls.log` does not exist.

## The help text

```bash
python3 apfs_tm_snapfree.py --help
```

Expected: exit `0`, the defaults `/etc/fstab` and `90` beside `--fstab` and
`--warn-percent`, and the prerequisites and exit codes in the closing paragraph.

## A usage error

```bash
: "${real:?run the commands under Make a throwaway folder first}"
python3 apfs_tm_snapfree.py --mode status --path "$real/throwaway" --apply
```

Expected: exit `2`, with `--apply does not apply to --mode status` on stderr. Nothing is read
but the command line.

## Clean up

```bash
: "${real:?run the commands under Make a throwaway folder first}"
rm -r "$real"
```
