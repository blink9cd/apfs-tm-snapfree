# apfs-tm-snapfree

Moves a folder whose contents churn (git worktrees, build output) onto its own APFS volume
in the same container, mounted at the same path and excluded whole from Time Machine, so
Time Machine's hourly local snapshots stop pinning the data it deletes. It also checks that
arrangement (`status`), changes the volume's size (`resize`) and undoes it (`teardown`).
Standard library only, on the stock `python3` of a Mac (3.9) or later.

Every mode is a dry run that prints its numbered steps, each as the exact command line it
runs; `--apply` performs them. A step that fails stops the run and prints the commands that
would undo what was done, and none of them is ever run for you.

Worked, read-only runs are in [`samples/`](samples/README.md).

## Why this exists

A Time Machine exclusion only keeps a folder out of the backup. The hourly local snapshot is
an APFS *volume* snapshot, so it freezes every block of the volume, the excluded folder's
included, and every file created and then deleted there stays allocated until each snapshot
taken while it existed expires. `du` then adds up to far less than `df` reports used. A volume
excluded as a whole gets no local snapshots at all, and a new volume in the same container
costs no partitioning: volumes in one container share its free space, and `-quota` only caps
how much this one may take. Measured: an hourly snapshot taken half a minute after such a
volume was mounted covered the main data volume and listed nothing for the new one.

Three things make the move fiddly by hand, and the tool handles each:

- **Mounting over a folder hides its contents** rather than merging with them, so the folder
  is moved aside first and its data copied onto the new volume.
- **A plain `diskutil mount` does not survive a reboot.** The tool adds an `/etc/fstab` line
  keyed by the volume's UUID, with the path written literally.
- **A volume that fails to mount fails silently**, and whatever writes to the path then writes
  into the hidden folder underneath. `status` checks for exactly that.

The volume is mounted at the folder's own path rather than reached through a symlink, so tools
that compare resolved paths still agree on where things live. Git, for one, records a
worktree's location as an absolute path in both directions, so worktrees under the folder need
no `git worktree repair`.

## Requirements

- macOS, with the folder on an APFS volume. The tool refuses to run anywhere else.
- Python 3.9 or newer: the `python3` the Command Line Tools install is enough
  (`xcode-select --install` if `python3` asks for them). Nothing else to install, and
  `dependencies` in `pyproject.toml` is empty.
- An administrator account, for `sudo`. Run the tool as yourself, never as root.
- Full Disk Access for your terminal app, which an applied setup, resize or teardown checks
  for before its first step; see
  [What to know before `--apply`](#what-to-know-before---apply).

Every command below is run from the repository root.

## Command line

| Option | Modes | Default | Meaning |
| --- | --- | --- | --- |
| `--mode` | all | required | `setup`, `status`, `resize` or `teardown` |
| `--path` | all | required | the folder, resolved through symlinks before use, since the fstab and the mount see the real path |
| `--quota` | setup, resize | required there | size ceiling: a whole number and `k`, `m`, `g` or `t`, powers of 1000 as diskutil reads them (`40g` is 40,000,000,000 bytes) |
| `--name` | setup | `snapfree-` and the folder's name | the new volume's name |
| `--fstab` | all | `/etc/fstab` | the fstab read and, with `--apply`, rewritten |
| `--warn-percent` | status | `90` | use of the quota at or above this whole percentage, 1 to 100, is "needs attention" |
| `--finish` | resize | off | delete the volume a previous resize retired; takes no `--quota` |
| `--apply` | setup, resize, teardown | off | act instead of printing the plan |

An option given to a mode it does not apply to is a usage error, as is `--quota` missing
from `setup` or a plain `resize`. The tool refuses to run off macOS or as root: it runs as
you and prefixes `sudo` to exactly the steps that need it, so sudo prompts on the terminal
and ownership goes back to you without guessing at `SUDO_UID`.

## Status: `--mode status`

Read-only, and never runs `sudo`. Each check prints one line, `ok|WARN|FAIL <check>:
<detail>`. FAIL means the state is known to be wrong; WARN means it needs a look.

1. **fstab line:** a line whose mount point is the folder, octal escapes (`\040` for a space)
   decoded and both sides resolved; the first such line wins, as at boot. None: the line
   reads "not configured", nothing else prints, and the exit is `2`.
2. **mounted:** the folder's `st_dev` differs from its parent's. `df` and `mount` are never
   parsed.
3. **right volume:** the volume holding the folder, named by its device from `stat`, is the
   one whose `diskutil info -plist` reports the `VolumeUUID` the fstab line names, compared
   ignoring case.
4. **Time Machine excluded:** `tmutil isexcluded -X` reports the folder excluded. This proves
   the outcome, not the tool's exclusion: Time Machine treats a volume mounted `nobrowse` as
   excluded with or without one, and status cannot read the explicit list (below) without
   `sudo`, which it never runs.
5. **Spotlight off:** `mdutil -s` says "disabled". This is the one prose parse, since mdutil
   has no structured output; text that says neither is WARN "unknown", never ok. A volume this
   tool made reads "Indexing and searching disabled.", thanks to the Spotlight marker setup and
   resize put on it (below); one made before the marker may read "unknown indexing state".
6. **ownership honored:** `diskutil info`'s `GlobalPermissionsEnabled` is true.
7. **use against quota:** the volume's `CapacityInUse` against its `CapacityQuota`, from
   `diskutil apfs list -plist`. No quota is WARN, and use at or over `--warn-percent` is WARN.
8. **retired volume pending:** a marker a resize left in the fstab is WARN, naming the
   `--finish` command that deletes the volume it kept.

When check 2 fails, checks 3 to 7 print WARN `skipped (not mounted)`: they would read the
parent volume and describe the wrong thing. A host command that fails during a check becomes
that check's FAIL line, so the other checks still report.

**How every mode reads a volume.** `diskutil info` takes a device, a volume UUID or a mount
point, and refuses any other folder with "Could not find disk". So a path is never handed to
it: the device of the volume holding the path comes first, from the system's BSD stat
(`/usr/bin/stat -L -f %Sd`, which prints a name such as `disk3s5`; a GNU stat earlier on `PATH`
reads those options differently), and diskutil is asked about that device. A path whose
volume is not on a disk (devfs, a network share) is refused. `--finish` asks diskutil about
the retired volume by its UUID, which it takes as it is.

## Setup: `--mode setup --path P --quota Q`

**Validation, before anything changes.** P exists, is a folder you own, and nothing is mounted
at it; the fstab has no line for it; its volume is APFS, and the container comes from
`diskutil info`'s `APFSContainerReference`, never assumed; no volume of the new name exists in
that container; the data in P (every file and symlink, `lstat` sizes, links not followed) is
under Q, and fits the container's free space with a 1 GB margin; the aside path
`P.snapfree-aside-<stamp>` is free (the stamp is the UTC time, `YYYYmmddHHMMSS`); and a
`.metadata_never_index` at P's top, if there is one, is an empty file (anything else is
refused, saying it is not the empty file this tool keeps there and to move it aside or rename
it first). A refusal names its reason and exits `2`. The Full Disk Access check (below)
comes last.

**Steps, in safe-failure order.** After each step the data is still whole in a known place,
and the fstab is written only once the volume has been proven to mount.

1. `sudo diskutil apfs addVolume <container> APFS <name> -quota <Q> -nomount`. Confirmed by
   finding exactly one volume of that name in the container, whose UUID the later steps use.
2. `mv P <aside> && mkdir -m <mode> P`: rename the folder aside, which on one volume is
   instant and costs no space, and recreate it empty with its original mode.
3. `sudo diskutil mount -mountPoint P <uuid>`: visible, the one kind of mount the tool makes
   without `nobrowse` (below), so that step 6's exclusion takes. Refused first if P gained
   entries since step 2, since the mount would hide them. Confirmed by `st_dev` and by the
   `VolumeUUID` at P.
4. `sudo diskutil enableOwnership P`, only if the new volume ignores ownership; confirmed.
5. `sudo chown <uid>:<gid> P && sudo chmod <mode> P`, confirmed by `stat`.
6. `sudo tmutil addexclusion -v P`, while the volume is still mounted visible: on a
   `nobrowse` mount it exits 0 and adds nothing. Confirmed by Time Machine's own list of
   excluded volumes, which must name the new volume's UUID (see *Time Machine's exclusions*
   below). It prints no recovery of its own: every undo after it ends in deleting the volume,
   and a deleted volume's UUID left in that list is harmless.
7. `touch P/.metadata_never_index`, made as you: the empty Spotlight marker (below). One
   already there is kept if it is an empty file and refused otherwise; confirmed by `lstat`.
8. `sudo diskutil unmount P && sudo diskutil mount -mountOptions nobrowse -mountPoint P
   <uuid>`: the remount that makes the marker take, since macOS reads it only as a volume
   mounts, and that hides the volume with `nobrowse` from then on. A busy volume is waited
   out (below). Between the two, refused if P gained entries while the volume was unmounted,
   as in step 3: the run names them and says to move them into the aside. Confirmed by
   `st_dev` and the `VolumeUUID` at P. If the step stops once the unmount has taken, the
   recovery block starts with that mount command, marked `# step 8's command ran`, to put
   the volume back at P.
9. `sudo mdutil -i off P`, judged by the `mdutil -s` read after it, not by its exit code:
   "disabled" passes even when `-i off` exited non-zero (it exits 1 on a volume the marker has
   already kept out of Spotlight), and "enabled" fails, naming both. Text it cannot read passes
   when `-i off` exited zero, left to `status` to report; after a non-zero exit nothing shows
   Spotlight is off, so it fails with both outputs.
10. Add `UUID=<uuid> <escaped P> apfs rw,nobrowse` to the fstab (the rewrite is below), and
    read it back. `nobrowse` keeps the volume off the desktop and out of the Finder sidebar.
11. `ditto <aside>/ P/` as you, so every file keeps your ownership. Confirmed by comparing a
    manifest of both sides (every path, its kind, file sizes and link targets); the entries the
    new volume holds for itself (macOS's `.fseventsd`, and the marker) are left out unless the
    aside holds one of that name, so an empty marker your folder already held is copied as it
    is.
12. Print where the original is, the `rm -rf <aside>` command to delete it once you have
    checked the copy, and the `status` command to run after the next reboot.

Every follow-up command the tool prints (that `status` command, and the `--finish` command
resize, status and teardown name) reads `python3 apfs_tm_snapfree.py --mode ...`, under the
file's own name, so it pastes from the tool's folder; each word is shell-quoted, and a
non-default `--fstab` is carried along. The printed `--finish` command includes `--apply`.

**Setup keeps your original.** The aside copy stays where step 2 put it; the tool never
deletes it. Check the copy, then run the printed `rm -rf` yourself. Until then the aside
still occupies its space on the parent volume.

**The fstab rewrite, shared by every mode.** The new text goes to a temporary file only you
can read; then `sudo cp <fstab> <fstab>.apfs_tm_snapfree.bak` (skipped when there is no fstab
yet), `sudo install -m 0644 -o root -g wheel <temp> <fstab>.apfs_tm_snapfree.new` and `sudo mv
-f` that onto the fstab. The staged copy sits beside the fstab, so the final rename is atomic.
The text is read again when the step runs, not when the plan is built, so an edit made in
between is kept. A dry run shows the temporary file as `<temp>`.

## Resize: `--mode resize --path P --quota Q2`

diskutil has no verb that changes a volume's quota, so a resize copies the data onto a new
volume and keeps the old one until you have checked the copy.

**Validation.** The fstab has a line for P, a volume is mounted at P, and it is the one that
line names; no retired volume is pending; the old volume's `CapacityInUse` plus the 1 GB
margin fits under Q2 and in the container's free space; neither the new name nor the
temporary mount point is taken; and a `.metadata_never_index` at P's root, if there is one,
is an empty file, since the copy leaves the name out (refused, exit `2`, as in setup). Each
volume-private name (below) present at P's root must be a folder owned by root, the one
macOS made. Anything else of that name would be left behind by the copy, so the run refuses
(exit `2`), saying that `P/<name>` is not the macOS-managed folder of that name, that the
copy would leave it behind, and to move it aside or rename it first. The Full Disk Access
check (below) comes last.

Before the steps it prints what it will do, says to quit anything that writes to P (what it
writes once the copy starts stays on the old volume), names the volume-private folders present
(they and their contents, the Versions history in `.DocumentRevisions-V100` and the volume's
Trash included, are not carried to the new volume and stay on the old one, which `--finish`
deletes), and, when FileVault is on for P's parent, that the new volume will not be encrypted.

1. `sudo diskutil apfs addVolume <container> APFS <stem>-<stamp> -quota <Q2> -nomount`. The
   stem is the old volume's name with any `-<stamp>` a previous resize gave it dropped, so
   names do not grow. Confirmed, and its UUID recorded.
2. `mkdir <parent>/.<name>.snapfree-resize-<stamp>`, the hidden temporary mount point.
3. `sudo diskutil mount -mountPoint <temp> <new-uuid>`: visible, as setup's step 3 is, so
   that step 7's exclusion takes. Confirmed by the UUID there.
4. `sudo diskutil enableOwnership <temp>`, only if the new volume ignores ownership.
5. `sudo chown <uid>:<gid> <temp>`, matching P's root; confirmed by `stat`.
6. `sudo chmod <mode> <temp>`, likewise.
7. `sudo tmutil addexclusion -v <temp>`, while the new volume is still mounted visible,
   confirmed, as in setup's step 6, by Time Machine's list naming the new volume's UUID.
8. `touch <temp>/.metadata_never_index` as you: the new volume's own Spotlight marker, as in
   setup's step 7.
9. `sudo diskutil unmount <temp> && sudo diskutil mount -mountOptions nobrowse -mountPoint
   <temp> <new-uuid>`, the remount that makes the marker take and hides the volume, as in
   setup's step 8.
10. `sudo mdutil -i off <temp>`, judged by the `mdutil -s` read after it, as setup's step 9
    is.
11. Copy P's data into the temporary mount one top-level entry at a time,
    `ditto P/<entry> <temp>/<entry>`, leaving out the volume's own entries (below), the old
    volume's Spotlight marker among them. A top-level symlink is recreated as
    `ln -s -- <target> <temp>/<entry>` instead, because ditto follows a symlink named as its
    source. The entries are listed when the step prints and when it runs. Confirmed by
    comparing a manifest of both sides, without those entries: every relative path, its kind
    (folder, file, symlink, other), each file's size and each link's target. A mismatch says
    the copy at `<temp>` does not match P, names the first entry missing from each side, and
    says to quit whatever writes to P and resize again.
12. `sudo diskutil unmount <temp>`, confirmed by `st_dev`. A busy volume is waited out (below).
13. `rmdir <temp>`.
14. `sudo diskutil unmount P`: the old volume comes off the folder, still intact. Just
    before the command runs, P is compared once more with the manifest step 11 confirmed, so
    nothing written after the copy is stranded on the old volume, which `--finish` deletes:
    a difference stops the run there, before the unmount, saying that P no longer matches
    what was copied to the new volume, that something wrote to P after the copy, naming the
    difference, and saying to quit it, run the recovery commands printed below and resize
    again. A busy volume is waited out, as in step 12.
15. `sudo diskutil mount -mountOptions nobrowse -mountPoint P <new-uuid>`, confirmed by the
    UUID at P.
16. In the fstab, swap the line's UUID to the new volume and add the marker
    `# apfs_tm_snapfree: retired UUID=<old> for <escaped P>`; read back.
17. Print that the old volume is kept, its data intact, until `--finish`, and the `--finish`
    command to run once the new one is checked.

The old volume has no fstab line once step 16 is done, so a restart before `--finish` may
mount it automatically under `/Volumes/<name>`. Its data is still intact; `--finish` then
refuses, saying where it is mounted and printing the `sudo diskutil unmount` command to run
first.

**`--mode resize --path P --finish`** deletes that kept volume. Deleting a volume has no undo,
so validation first proves the data lives elsewhere: the marker exists; the fstab has a line
for P; the volume mounted at P is the one that line names, and not the retired one; and no
fstab line, for any mount point, names the retired volume, since the next boot would mount it
there (remove or change such a line first, with `sudo vifs`). The retired volume must then
look like what a resize leaves behind: in the same container as P's volume, named from the
same stem, with no APFS role, and mounted nowhere. The Full Disk Access check
(below) comes last. Its three steps are `sudo diskutil apfs deleteVolume <old>`, confirmed gone
from `diskutil apfs list`; the marker's removal from the fstab; and a read of Time Machine's
list of excluded volumes. If the marker cannot be removed after the volume is gone, the
recovery line says to remove it by hand with `sudo vifs`. The read only reports: a resize
leaves the old volume's UUID in that list, and no `tmutil` command can remove it once the
volume is gone, so when it is still there the run prints a note calling the entry harmless and
naming System Settings > General > Time Machine > Options, where exclusions can be reviewed.
The tool never edits Time Machine's preferences itself. A failed read prints only itself, as
the retry.

## Teardown: `--mode teardown --path P`

**Validation.** The fstab has a line for P, the volume it names is the one mounted at P, no
retired volume is pending (finish the resize first), the data fits the parent container's free
space with the margin, and the restore folder's name is free. As in resize, each
volume-private name present at P's root must be the root-owned folder macOS made, and a
`.metadata_never_index` there must be an empty file, or the run refuses (exit `2`). The Full
Disk Access check (below) comes last.

Before the steps it prints what it will move, and says to quit anything that writes to P: what
it writes once the copy starts stays on the volume, which teardown deletes, along with
anything in the volume's own Trash. It then names the volume-private folders present: they and
their contents, the Versions history in `.DocumentRevisions-V100` and the volume's Trash
included, are not copied and go with the volume when teardown deletes it. When P's root
holds the Spotlight marker, it says that too: the marker is not copied, goes with the volume,
and Spotlight then treats P as the parent volume says. That holds for an empty
`.metadata_never_index` your folder held before setup as well, since the two cannot be told
apart: the plain folder teardown leaves has none.

1. `mkdir -m <mode> <parent>/.<name>.snapfree-restore-<stamp>` on the parent volume, then copy
   each top-level entry into it as resize's step 11 does; confirmed by the same manifest
   comparison.
2. `sudo tmutil removeexclusion -v P`, confirmed by Time Machine's list of excluded volumes
   no longer naming the volume's UUID. `tmutil isexcluded` cannot confirm it: P is still
   mounted `nobrowse`, which it reports excluded whatever the exclusion. Unlike
   `addexclusion -v` (see *Time Machine's exclusions* below), `removeexclusion -v` does take
   on a `nobrowse` mount: a live run on macOS 27 saw the volume's UUID leave the list. If the
   confirm ever fails, it fails the step before anything is unmounted, with the data still
   whole in both places.
3. `sudo diskutil unmount P`, confirmed by `st_dev`. Just before the command runs, P is
   compared with the copy once more, so nothing written after the copy is hidden by the unmount
   and deleted with the volume: a difference stops the run there, before the unmount, saying
   that something wrote to P after the copy, naming the difference, and saying to quit it, run
   the recovery commands printed below and teardown again. A busy volume is waited out
   (below); one still busy after that means something has P open,
   and the error names it where diskutil does, and says that `lsof +D P` lists what.
4. The unmount uncovers the folder the volume was mounted over. Empty, it is removed; holding
   anything (written while the volume was not mounted), it is moved to
   `P.snapfree-hidden-<stamp>` and the run says so. It is never deleted.
5. `mv <restore> P`, confirmed to be a plain folder with nothing mounted on it.
6. Remove the fstab line, and read the fstab back without it.
7. `sudo diskutil apfs deleteVolume <uuid>`, confirmed gone. It runs once the data is proven
   restored, so if it fails the run prints only this step to retry: undoing steps 1 to 6 would
   take the restored folder apart.
8. `tmutil isexcluded -X P`, a check only: the plain folder must read not excluded, which is
   where `isexcluded` answers the question that matters. If it reads excluded, the run fails
   here saying the data is safe in P but Time Machine excludes it, to see why with `tmutil
   isexcluded -X P`, and that `sudo tmutil removeexclusion -p P` is the likely fix; there is
   nothing to retry or undo.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | `status`: every check is ok. Every other mode: every step was printed (dry run) or completed (`--apply`) |
| `1` | `status`: any check is WARN or FAIL, a host command failing during a check included. Every other mode: a step failed under `--apply`; its recovery commands are printed and none is run |
| `2` | a usage error; a run refused before any check or step (off macOS, as root, a validation that failed, a host command or read failing while validating, an `--apply` run without Full Disk Access); and for `status`, a folder the fstab does not configure |

## Recovery is printed, never run

When a step fails, the run prints `FAILED at step N`, then the undo command of every
completed step, most recent first. If the failed step's own command ran but its confirm did
not see the result (a volume the confirm could not find, say), that step's undo prints first,
marked `# step N's command ran`, because the command may have changed something. Typical
lines are `sudo diskutil unmount P`, `rmdir P && mv <aside> P`, `sudo diskutil apfs
deleteVolume <uuid>`, and `sudo cp <fstab>.apfs_tm_snapfree.bak <fstab>`, each with the real
UUIDs and paths learned by then. None is run automatically: a half-finished state is better
judged by a person than by an undo that guesses at it.

Teardown's step 2, which removes the volume's Time Machine exclusion, prints a comment
instead of an undo. Until step 6 removes P's fstab line, diskutil applies that line's
`nobrowse` to a mount of the volume even when it is given no `-mountOptions`, so no mount
this tool prints is visible; and `sudo tmutil addexclusion -v P` exits 0 and adds nothing on a
`nobrowse` mount (see *Time Machine's exclusions* below). Nothing is lost meanwhile: Time
Machine skips a volume mounted `nobrowse` whether or not it has an exclusion of its own.
`status` reads it excluded either way, since `isexcluded` reports every `nobrowse` volume
excluded, so it cannot tell you whether the exclusion of its own is still on record.

## What to know before `--apply`

- **Quit whatever writes to the folder first**, in every mode. Every mode confirms its copy by
  comparing a manifest of both sides (paths, kinds, file sizes, link targets), and teardown
  compares again just before it unmounts. So a write that adds, removes, renames, or resizes
  something fails the check. A write that rewrites a file in place at the same size is not
  caught, since neither file contents nor modification times are compared, and a write that
  lands after the copy lands on the side that is about to be retired.
- **Snapshots already taken still pin data until they expire.** Setup stops new snapshots
  covering the folder, but the ones already taken still hold everything the folder held when
  they were taken, and the aside copy, a retired volume and a torn-down volume's data the same
  way. So "the space did not come back today" is expected; it returns as those snapshots age
  out, normally about a day after a backup completes (`tmutil listlocalsnapshots /`).
- **The new volume is not encrypted**, even with FileVault on: a volume mounted at boot cannot
  ask for a password. Setup and resize say so before their steps when FileVault is on. Do not
  put anything there that needs encryption at rest.
- **Full Disk Access.** macOS refuses `sudo tmutil addexclusion -v` (setup's step 6,
  resize's step 7) unless the terminal app running the tool has Full Disk Access, and
  teardown's `removeexclusion -v` (step 2) is taken to need it too, as does reading Time
  Machine's list of excluded volumes, under `sudo`, which confirms those steps and is
  `resize --finish`'s step 3. The refusal would come only after the earlier steps had run, so
  setup, resize, `resize --finish` and teardown check first, after
  validation: they read one byte of `/Library/Preferences/com.apple.TimeMachine.plist` as
  you. Its mode lets anyone read it, but macOS refuses the read ("Operation not permitted")
  without the grant. Refused, an `--apply` run stops before step 1 with exit `2`, naming the
  app (Terminal, or whatever `TERM_PROGRAM` calls it) and where to grant it, System Settings >
  Privacy & Security > Full Disk Access, after which the app must be quit and reopened. A dry
  run prints the same as a warning and still lists its steps. With no such file (Time
  Machine never set up) the check cannot tell, and the run goes on. A command whose error
  reads "Operation not permitted" still gets a second line naming the likely cause. The tool
  never changes privacy settings.
- **Time Machine's exclusions.** Time Machine treats a volume mounted `nobrowse`, as every
  volume this tool mounts is once its first mount is over, as excluded whether or not
  anything excluded it, and `tmutil isexcluded` reports that. So the steps that add or remove
  an exclusion are confirmed by Time Machine's own list instead, `ExcludedVolumeUUIDs` in
  `/Library/Preferences/com.apple.TimeMachine.plist`, read with `sudo /usr/bin/plutil -extract
  ExcludedVolumeUUIDs xml1 -o - <that file>` and compared ignoring case. No key, or no file,
  reads as an empty list; any other failure fails the step. tmutil's change can reach the file
  late, so a read that does not yet show it is retried after waits of 0.5, 1, 2, 4 and 8
  seconds, stopping at the first read that does; the step fails only after the last. The tool
  only ever reads the file. That list also showed, live on macOS 27, that `tmutil
  addexclusion -v` on a `nobrowse` mount exits 0 and adds nothing to it, so setup and resize
  exclude a new volume while its first mount is still visible. No recovery removes an
  exclusion setup or resize added: every undo after it ends in deleting the volume, and a
  deleted volume's UUID left in the list is harmless, as `resize --finish` notes.
- **Every mount but a new volume's first is `nobrowse`.** Each `diskutil mount` the tool runs
  or prints, recovery lines included, passes `-mountOptions nobrowse`, so the volume does not
  show in Finder and is not browsed by Spotlight, except a new volume's first mount (setup's
  step 3, resize's step 3). That one is visible, because the exclusion does not take on a
  `nobrowse` mount, and lasts only until the remount after the Spotlight marker, which mounts
  the volume `nobrowse` for good. diskutil already applies the fstab line's options to a
  volume that has one, but setup's volume has none until step 10, and resize's temporary mount
  never does.
- **Spotlight is kept off by a marker.** `mdutil -i off` alone left a fresh volume reading
  "unknown indexing state" on current macOS. An empty `.metadata_never_index` at the volume's
  root is still honored, but only when it is there as the volume mounts, and the disabled state
  then holds even without it. So setup and resize create it once the volume is yours, mount the
  volume again, and only then run `mdutil -i off`, which on such a volume exits 1, printing a
  "disabling Spotlight ... -> kMDConfigSearchLevelFSSearchOnly" log line, while `mdutil -s`
  reads disabled: so the read, not the exit code, is the step's verdict. The marker stays at
  the root for good; it is the tool's own, so no copy carries it and no comparison counts it,
  and resize gives the new volume its own rather than copying the old one's.
- **A busy unmount is waited out, never forced.** Spotlight's `mds` can hold a volume the
  tool has just made, even after `mdutil -i off`, and diskutil then refuses to unmount it,
  "dissented by" mds; it was seen only on a mount that was not `nobrowse`, which a new
  volume's first mount now is, and that is the mount the remounts unmount. So when an
  unmount (the remounts, setup's step 8 and resize's step 9; resize's steps 12 and 14;
  teardown's step 3) is refused with text saying it was dissented or the resource was busy,
  the tool reads whether the folder is still mounted, waits 1, 2, 4, 8, and then 16 seconds
  between tries, about half a minute in all, and stops as soon as the folder is unmounted.
  Any other refusal fails at once. Still busy after the last try, the step fails as any other
  does, naming the process diskutil named, and says to wait for it to let go or quit it before
  running the printed recovery. `diskutil unmount force` is never run for you.
- **The volume's own entries.** A mounted volume's root holds `.fseventsd`, `.Spotlight-V100`,
  `.Trashes`, `.TemporaryItems` and `.DocumentRevisions-V100`, which macOS keeps for the volume
  itself: root-only, and churning. Resize and teardown leave them out, at the volume root's top
  level only, when they copy and compare, as they do the Spotlight marker, and the same names
  deeper down are ordinary data.
  Because they go by name, both modes first prove each one present is a folder owned by root,
  and refuse anything else of that name rather than leave it behind. What they hold, the
  volume's Trash and the Versions history of documents on it included, is not carried across;
  empty the Trash or move what you need out first.
- **After a reboot, run `status`.** A volume that fails to mount does so silently, and
  whatever writes to the path then writes into the hidden folder underneath.

## Trying it on a throwaway folder

A first run is best made on a folder you can lose. It changes the real `/etc/fstab` (the
previous one is kept as `/etc/fstab.apfs_tm_snapfree.bak`) and makes a small volume, then
resizes it, and the teardown at the end undoes both.

```bash
mkdir "$HOME/snapfree-trial"
printf 'hello\n' > "$HOME/snapfree-trial/note.txt"
python3 apfs_tm_snapfree.py --mode setup --path "$HOME/snapfree-trial" --quota 2g
python3 apfs_tm_snapfree.py --mode setup --path "$HOME/snapfree-trial" --quota 2g --apply
python3 apfs_tm_snapfree.py --mode status --path "$HOME/snapfree-trial"
```

The first `setup` prints its twelve steps and changes nothing; the second performs them, asking
for your password once for sudo; `status` should print eight `ok` lines and exit `0`.
Restart, and run `status` again to see the fstab line mount the volume at boot.

Then give the volume a larger quota, check that the data came across, and delete the old
volume the resize keeps:

```bash
python3 apfs_tm_snapfree.py --mode resize --path "$HOME/snapfree-trial" --quota 3g
python3 apfs_tm_snapfree.py --mode resize --path "$HOME/snapfree-trial" --quota 3g --apply
python3 apfs_tm_snapfree.py --mode status --path "$HOME/snapfree-trial"
cat "$HOME/snapfree-trial/note.txt"
python3 apfs_tm_snapfree.py --mode resize --path "$HOME/snapfree-trial" --finish --apply
python3 apfs_tm_snapfree.py --mode status --path "$HOME/snapfree-trial"
```

The first `resize` prints its seventeen steps and changes nothing; the second performs them
and keeps the old volume, its data intact. The `status` after it prints seven `ok` lines and
a last one, `WARN retired volume pending`, naming the `--finish` command, and exits `1`.
`cat` should print `hello`, read from the new volume, before anything is deleted. `--finish`
performs its three steps and deletes the old volume, and the last `status` prints eight `ok`
lines again. Run `--finish` before
you next restart: the old volume has no fstab line, so a restart may mount it under
`/Volumes`, and `--finish` then refuses until it is unmounted (see
[Resize](#resize---mode-resize---path-p---quota-q2)).

Then undo it all:

```bash
python3 apfs_tm_snapfree.py --mode teardown --path "$HOME/snapfree-trial" --apply
```

Setup printed the `rm -rf` command for the aside copy, `snapfree-trial.snapfree-aside-<stamp>`
beside the folder; run it once you are satisfied, then delete the trial folder.

## Running the tests

The suites are standard-library `unittest` and run on Python 3.9, the `python3` of a stock
Mac, with nothing installed. There are two, and each needs its own command:

```bash
python3 -m unittest discover
python3 stdio_utf8/test_stdio_utf8.py
```

The first runs the tool's suites. `discover` walks only folders that are Python packages, and
`stdio_utf8/` is not one, so the library's suite runs from its own entry point, the second
command.

Every host command goes through one seam, `sf_host.Host`, and so do the Full Disk Access
probe, the environment it names the app from, and the waits between unmount retries. The
suites replace it with a scripted fake (`tests/fake_host.py`), so no test reaches a disk, a
volume, a system setting, or `sudo`. The one live test class runs the entry point as a child
process for `--help` and for `status` against a temporary fstab with no line for the folder,
both of which end before any host command. No test performs `--apply` on a real volume: that
proof is a real run on a throwaway folder, as above.

## Layout

| Path | What it holds |
| --- | --- |
| `apfs_tm_snapfree.py` | the entry point: options, the mode table, exit codes |
| `src/` | one module per concern, each named `sf_*`: the host seam, the fstab, the macOS readers, the plan runner, and one module per mode |
| `tests/` | the suites, one per module, and the scripted host they share |
| `test_apfs_tm_snapfree.py` | runs every suite under `tests/`, and checks that each one was found |
| `stdio_utf8/` | a small library that keeps the tool's output UTF-8 whatever the locale says, with its own suite |
| `samples/` | read-only runs to check by hand, with their expected output |

## License

AGPL-3.0-or-later; see `LICENSE`.
