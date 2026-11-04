#!/bin/bash
# SPDX-License-Identifier: LGPL-3.0-or-later
# Box-local EC maintenance for the SeaweedFS free version (it never vacuums EC volumes and has no EC repair on corruption). Runs ON the box
# from its own systemd timer (one box per night); it needs no ssh and no outside driver, only this box's mutual-TLS security.toml for `weed shell`.
#   scrub    ec.scrub -mode checksum on THIS box's shards; for each bad shard: stop this box's volume server, remove the shard file, start it,
#            ec.rebuild the volume, scrub the volume again.
#   cleanup  EC volumes owned by this box (volume id mod BOXES = INDEX) whose deleted share (entries in .ecj / entries in .ecx, read from this
#            box's index files) is above the threshold: ec.decode -> volume.vacuum -> ec.encode, ONE volume at a time, then a checksum scrub.
#            Stops at the first error.
# Both are DRY-RUN unless --apply. One run at a time per box (flock) and per cluster (the weed shell lock).
set -euo pipefail
MODE=${1:?usage: swfs-maint.sh scrub|cleanup --weed P --masters M --node H:P.G --index N --boxes N --data DIR --service UNIT [--apply] [--threshold 0.3]}; shift
APPLY=0; TH=0.3; WEED=; MASTERS=; NODE=; IDX=; BOXES=7; DATA=; SERVICE=seaweed-volume.service; NODES_EXPECTED=7
while [ $# -gt 0 ]; do case $1 in
  --apply) APPLY=1;; --threshold) TH=$2; shift;; --weed) WEED=$2; shift;; --masters) MASTERS=$2; shift;; --node) NODE=$2; shift;;
  --index) IDX=$2; shift;; --boxes) BOXES=$2; NODES_EXPECTED=$2; shift;; --data) DATA=$2; shift;; --service) SERVICE=$2; shift;;
  *) echo "unknown $1" >&2; exit 2;; esac; shift; done
for v in WEED MASTERS NODE IDX DATA; do [ -n "${!v}" ] || { echo "missing --${v,,}" >&2; exit 2; }; done
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
LOCK=${DATA%/*}/swfs-maint.lock
exec 9>"$LOCK"; flock -n 9 || { echo "another run holds the lock" >&2; exit 3; }
event() { echo "$(date -Iseconds) $MODE box_index=$IDX apply=$APPLY $*"; }
trap 'rc=$?; [ $rc -ne 0 ] && event "FAILED exit $rc"' EXIT
W() { printf '%s\n' "$1" | "$WEED" shell -master="$MASTERS" 2>&1; }
scrub_vol() { W "lock
ec.scrub -mode checksum -details -volumeId $1 -node $NODE
unlock"; }
# Hard guard: never decode/encode/repair while a box is down or an EC volume has fewer than 14 shards.
guard() {
  local lst; lst=$(W "volume.list")
  echo "$lst" | EXPECT=$NODES_EXPECTED python3 -c "
import os,re,sys,collections
t=sys.stdin.read(); ec=collections.defaultdict(set)
for l in t.splitlines():
    m=re.search(r'ec volume id:(\d+).*?shards:\[([\d ]*)\]',l)
    if m: ec[m.group(1)] |= set(m.group(2).split())
if 'total size' not in t: print('GUARD: volume.list incomplete'); sys.exit(1)
nodes=set(re.findall(r'DataNode (\S+)',t))
if len(nodes)!=int(os.environ['EXPECT']): print('GUARD: only',len(nodes),'volume servers registered'); sys.exit(1)
bad={v:len(s) for v,s in ec.items() if len(s)<14}
if bad: print('GUARD: EC volumes with fewer than 14 shards:',bad); sys.exit(1)
" || { event "SKIP guard tripped"; trap - EXIT; exit 4; }
}

case $MODE in
cleanup)
  ratios=$(cd "$DATA" && for x in *.ecx; do [ -e "$x" ] || continue; b=${x%.ecx}; j=0; [ -f "$b.ecj" ] && j=$(stat -c %s "$b.ecj"); echo "EC $b $(stat -c %s "$x") $j"; done \
    | awk '$1=="EC"{e=$3/16; d=$4/8; printf "%s %d %d %.3f\n", $2, e, d, (e>0? d/e:0)}' | sort -k4 -nr)
  echo "$ratios" | sed 's/^/volume entries deleted ratio: /'
  echo "$ratios" | awk -v t="$TH" -v idx="$IDX" -v n="$BOXES" '{k=split($1,a,"_"); v=a[k]+0} $4>t && v%n==idx{print $1}' | while read -r b; do
    coll=${b%_*}; vid=${b##*_}
    if [ $APPLY = 0 ]; then echo "DRY-RUN would run: ec.decode -collection=$coll -volumeId=$vid; volume.vacuum; ec.encode -shardReplicaPlacement=020; scrub"; continue; fi
    guard
    echo "== $b"; W "lock
ec.decode -collection=$coll -volumeId=$vid
unlock" | tail -1
    for _ in $(seq 1 24); do W "volume.list" | grep -q "volume Id:$vid," && break; sleep 5; done   # the master learns the decoded volume with the next heartbeat
    W "lock
volume.vacuum -collection=$coll -volumeId=$vid -garbageThreshold=0.001
ec.encode -collection=$coll -volumeId=$vid -shardReplicaPlacement=020
unlock" | tail -2
    scrub_vol "$vid" | grep -E "Scrubbed|failures|mismatch" || true
  done ;;
scrub)
  out=$(W "lock
ec.scrub -mode checksum -details -node $NODE
unlock")
  echo "$out" | grep -E "Scrubbed|failures|mismatch" || true
  # a clean scrub has no mismatch line: grep exits 1 then, which under `set -e -o pipefail` would end the run as FAILED; `|| true` keeps the loop empty
  { echo "$out" | grep -E "ec volume [0-9]+ shard [0-9]+: checksum mismatch" || true; } | sed -E 's/^\s*\[[0-9.]+:[0-9]+\] ec volume ([0-9]+) shard ([0-9]+):.*/\1 \2/' | sort -u | while read -r vid shard; do
    coll=$(cd "$DATA" && ls | grep -E "_$vid\.ecx\$" | sed "s/_$vid.ecx//" | head -1)
    f=$(printf '%s_%s.ec%02d' "$coll" "$vid" "$shard")
    if [ $APPLY = 0 ]; then echo "DRY-RUN would repair: stop $SERVICE, remove $f, start, ec.rebuild -collection=$coll -volumeIds=$vid -apply, re-scrub"; continue; fi
    guard
    systemctl --user stop "$SERVICE"; rm -f "$DATA/$f"; systemctl --user start "$SERVICE"; sleep 25
    W "lock
ec.rebuild -collection=$coll -volumeIds=$vid -apply
unlock" | tail -2
    scrub_vol "$vid" | grep -E "Scrubbed|failures|mismatch" || true
  done ;;
*) echo "mode?" >&2; exit 2;; esac
event "done"
