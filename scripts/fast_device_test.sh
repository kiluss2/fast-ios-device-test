#!/usr/bin/env bash

set -euo pipefail

PROJECT_PATH="${FAST_IOS_PROJECT:-}"
SCHEME="${FAST_IOS_SCHEME:-}"
CONFIGURATION="${FAST_IOS_CONFIGURATION:-Debug}"
DEVICE_SELECTOR="${FAST_IOS_DEVICE_ID:-}"
CACHE_ROOT="${FAST_IOS_DEVICE_TEST_CACHE:-}"
BUILD_MODE="auto"
BUILD_ONLY=false
DRY_RUN=false
QUIET=true
ALLOW_PACKAGE_RESOLUTION=false
ONLY_TESTING=()

usage() {
    cat <<'EOF'
Usage:
  fast_device_test.sh --project <xcodeproj-or-xcworkspace> --scheme <name> [options]

Options:
  --project <path>       Path to an .xcodeproj or .xcworkspace.
  --scheme <name>        Shared Xcode scheme to test.
  --configuration <name> Build configuration. Default: Debug.
  --device <value>       iPhone name, CoreDevice identifier, or hardware UDID.
  --only-testing <id>    Restrict tests; repeat for multiple identifiers.
  --reuse-build          Reuse cached products without fingerprint checking.
  --force-build          Run incremental build-for-testing before tests.
  --build-only           Prepare cached test products without running tests.
  --cache <path>         Override the cache root.
  --allow-package-resolution
                         Let Xcode update package resolution during the build.
  --list-devices         Show CoreDevice devices and exit.
  --dry-run              Print commands without building or installing.
  --verbose              Disable xcodebuild -quiet.
  -h, --help             Show this help.
EOF
}

fail() {
    echo "error: $*" >&2
    exit 1
}

require_value() {
    [[ $# -ge 2 && -n "$2" ]] || fail "$1 requires a value"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
    --project)
        require_value "$@"
        PROJECT_PATH="$2"
        shift 2
        ;;
    --scheme)
        require_value "$@"
        SCHEME="$2"
        shift 2
        ;;
    --configuration)
        require_value "$@"
        CONFIGURATION="$2"
        shift 2
        ;;
    --device)
        require_value "$@"
        DEVICE_SELECTOR="$2"
        shift 2
        ;;
    --only-testing)
        require_value "$@"
        ONLY_TESTING+=("$2")
        shift 2
        ;;
    --reuse-build)
        BUILD_MODE="reuse"
        shift
        ;;
    --force-build)
        BUILD_MODE="force"
        shift
        ;;
    --build-only)
        BUILD_ONLY=true
        shift
        ;;
    --cache)
        require_value "$@"
        CACHE_ROOT="$2"
        shift 2
        ;;
    --allow-package-resolution)
        ALLOW_PACKAGE_RESOLUTION=true
        shift
        ;;
    --list-devices)
        xcrun devicectl list devices
        exit 0
        ;;
    --dry-run)
        DRY_RUN=true
        shift
        ;;
    --verbose)
        QUIET=false
        shift
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        fail "unknown option: $1"
        ;;
    esac
done

command -v xcodebuild >/dev/null || fail "xcodebuild was not found"
command -v xcrun >/dev/null || fail "xcrun was not found"
[[ -n "$PROJECT_PATH" ]] || fail "--project is required"
[[ -n "$SCHEME" ]] || fail "--scheme is required"

PROJECT_PATH="$(cd "$(dirname "$PROJECT_PATH")" && pwd)/$(basename "$PROJECT_PATH")"
[[ -e "$PROJECT_PATH" ]] || fail "project/workspace does not exist: $PROJECT_PATH"
case "$PROJECT_PATH" in
*.xcodeproj)
    CONTAINER_ARGUMENT=(-project "$PROJECT_PATH")
    ;;
*.xcworkspace)
    CONTAINER_ARGUMENT=(-workspace "$PROJECT_PATH")
    ;;
*)
    fail "--project must point to an .xcodeproj or .xcworkspace"
    ;;
esac

PROJECT_ROOT="$(dirname "$PROJECT_PATH")"
[[ -n "$CACHE_ROOT" ]] || CACHE_ROOT="$PROJECT_ROOT/build/fast-device-tests"

device_rows() {
    xcrun devicectl list devices 2>/dev/null \
        | awk '/iPhone/ && ($0 ~ /connected/ || $0 ~ /available \(paired\)/) { print }'
}

extract_core_identifier() {
    grep -Eo '[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}' \
        | head -n 1
}

hardware_udid_for_core_identifier() {
    local core_identifier="$1"
    xcrun devicectl device info details --device "$core_identifier" 2>/dev/null \
        | awk '/udid:/ { print $NF; exit }'
}

resolve_device_udid() {
    local selector="$1"
    local matching_rows=()
    local row=""
    local core_identifier=""
    local hardware_udid=""

    if [[ "$selector" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{16}$ ]]; then
        echo "$selector"
        return
    fi

    if [[ "$selector" =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; then
        hardware_udid="$(hardware_udid_for_core_identifier "$selector")"
        [[ -n "$hardware_udid" ]] || fail "could not resolve hardware UDID for $selector"
        echo "$hardware_udid"
        return
    fi

    while IFS= read -r row; do
        [[ -n "$row" ]] && matching_rows+=("$row")
    done < <(device_rows | grep -iF -- "$selector" || true)

    if [[ ${#matching_rows[@]} -eq 0 ]]; then
        fail "no available iPhone matched '$selector'; use --list-devices"
    fi
    if [[ ${#matching_rows[@]} -gt 1 ]]; then
        echo "Multiple iPhones matched '$selector':" >&2
        printf '  %s\n' "${matching_rows[@]}" >&2
        fail "use a more specific name or UDID"
    fi

    core_identifier="$(printf '%s\n' "${matching_rows[0]}" | extract_core_identifier)"
    [[ -n "$core_identifier" ]] || fail "could not parse CoreDevice identifier"
    hardware_udid="$(hardware_udid_for_core_identifier "$core_identifier")"
    [[ -n "$hardware_udid" ]] || fail "could not resolve hardware UDID for $core_identifier"
    echo "$hardware_udid"
}

if [[ -z "$DEVICE_SELECTOR" ]]; then
    CONNECTED_ROWS=()
    while IFS= read -r row; do
        [[ -n "$row" ]] && CONNECTED_ROWS+=("$row")
    done < <(xcrun devicectl list devices 2>/dev/null | awk '/iPhone/ && /connected/ { print }')

    if [[ ${#CONNECTED_ROWS[@]} -eq 1 ]]; then
        DEVICE_SELECTOR="$(printf '%s\n' "${CONNECTED_ROWS[0]}" | extract_core_identifier)"
    elif [[ ${#CONNECTED_ROWS[@]} -eq 0 ]]; then
        fail "no connected iPhone; connect one or pass --device"
    else
        echo "More than one iPhone is connected:" >&2
        printf '  %s\n' "${CONNECTED_ROWS[@]}" >&2
        fail "pass --device <name-or-udid> to avoid testing the wrong phone"
    fi
fi

DEVICE_UDID="$(resolve_device_udid "$DEVICE_SELECTOR")"
SAFE_CACHE_KEY="$(printf '%s-%s-%s' "$SCHEME" "$CONFIGURATION" "$DEVICE_UDID" | tr -cs '[:alnum:]._' '-')"
DERIVED_DATA_PATH="$CACHE_ROOT/$SAFE_CACHE_KEY/DerivedData"
FINGERPRINT_FILE="$CACHE_ROOT/$SAFE_CACHE_KEY/source.fingerprint"
LOG_DIR="$CACHE_ROOT/$SAFE_CACHE_KEY/logs"

project_fingerprint() {
    find "$PROJECT_ROOT" \
        \( -type d \( \
            -name .git -o \
            -name .build -o \
            -name build -o \
            -name DerivedData -o \
            -name Pods -o \
            -name node_modules \
        \) -prune \) -o \
        \( -type f \( \
            -name '*.swift' -o -name '*.m' -o -name '*.mm' -o -name '*.h' -o \
            -name '*.c' -o -name '*.cc' -o -name '*.cpp' -o -name '*.plist' -o \
            -name '*.xcconfig' -o -name '*.entitlements' -o -name '*.json' -o \
            -name '*.strings' -o -name '*.xcstrings' -o -name '*.storyboard' -o \
            -name '*.xib' -o -name '*.pbxproj' -o -name '*.xcscheme' -o \
            -name 'Package.resolved' -o -name '*.png' -o -name '*.jpg' -o \
            -name '*.jpeg' -o -name '*.gif' -o -name '*.webp' -o -name '*.pdf' -o \
            -name '*.svg' -o -name '*.mp3' -o -name '*.wav' -o -name '*.mp4' -o \
            -name '*.otf' -o -name '*.ttf' -o -name '*.xcprivacy' \
        \) -print0 \) \
        | xargs -0 stat -f '%m:%z:%N' \
        | LC_ALL=C sort \
        | shasum -a 256 \
        | awk '{ print $1 }'
}

has_cached_test_products() {
    [[ -d "$DERIVED_DATA_PATH/Build/Products" ]] \
        && find "$DERIVED_DATA_PATH/Build/Products" -name '*.xctestrun' -print -quit \
            | grep -q .
}

print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

run_logged() {
    local label="$1"
    shift
    local command=("$@")
    local log_path="$LOG_DIR/$(date '+%Y%m%d-%H%M%S')-$label.log"
    local started_at=$SECONDS
    local status=0

    echo "[$label]"
    print_command "${command[@]}"
    if $DRY_RUN; then
        return
    fi

    mkdir -p "$LOG_DIR"
    set +e
    "${command[@]}" 2>&1 | tee "$log_path"
    status=${PIPESTATUS[0]}
    set -e

    if [[ $status -ne 0 ]]; then
        echo "[$label] failed after $((SECONDS - started_at))s" >&2
        echo "Log: $log_path" >&2
        exit "$status"
    fi
    echo "[$label] passed in $((SECONDS - started_at))s"
    echo "Log: $log_path"
}

mkdir -p "$CACHE_ROOT/$SAFE_CACHE_KEY"
CURRENT_FINGERPRINT=""
CACHED_FINGERPRINT=""
[[ -f "$FINGERPRINT_FILE" ]] && CACHED_FINGERPRINT="$(<"$FINGERPRINT_FILE")"

NEEDS_BUILD=false
case "$BUILD_MODE" in
reuse)
    has_cached_test_products || fail "no cached test products; run once without --reuse-build"
    ;;
force)
    CURRENT_FINGERPRINT="$(project_fingerprint)"
    NEEDS_BUILD=true
    ;;
auto)
    CURRENT_FINGERPRINT="$(project_fingerprint)"
    if ! has_cached_test_products || [[ "$CURRENT_FINGERPRINT" != "$CACHED_FINGERPRINT" ]]; then
        NEEDS_BUILD=true
    fi
    ;;
esac

COMMON_ARGUMENTS=(
    "${CONTAINER_ARGUMENT[@]}"
    -scheme "$SCHEME"
    -configuration "$CONFIGURATION"
    -destination "platform=iOS,id=$DEVICE_UDID"
    -derivedDataPath "$DERIVED_DATA_PATH"
    -skipPackagePluginValidation
    -allowProvisioningUpdates
    COMPILER_INDEX_STORE_ENABLE=NO
)
$ALLOW_PACKAGE_RESOLUTION || COMMON_ARGUMENTS+=(-disableAutomaticPackageResolution)
$QUIET && COMMON_ARGUMENTS+=(-quiet)

# SwiftLint is not the test's business: a no-op `swiftlint` (a function for sh/bash run-script
# phases, which wins over a phase's own PATH, and a shim first on PATH) unless FAST_UI_SKIP_LINT=0.
LINT_ENV=()
case "${FAST_UI_SKIP_LINT:-1}" in
0 | false | no | "") ;;
*)
    SHIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/shims"
    LINT_ENV=(env "PATH=$SHIM_DIR:$PATH"
        "BASH_FUNC_swiftlint%%=() { echo 'note: swiftlint skipped by fastDeviceTest (FAST_UI_SKIP_LINT=0 lints)'; return 0; }")
    ;;
esac

echo "Device: $DEVICE_UDID"
echo "Cache:  $DERIVED_DATA_PATH"

if $NEEDS_BUILD; then
    run_logged build-for-testing \
        ${LINT_ENV[@]+"${LINT_ENV[@]}"} xcodebuild build-for-testing "${COMMON_ARGUMENTS[@]}"
    if ! $DRY_RUN; then
        printf '%s\n' "$CURRENT_FINGERPRINT" > "$FINGERPRINT_FILE"
    fi
else
    echo "[build-for-testing] skipped; cached products match project inputs"
fi

if $BUILD_ONLY; then
    exit 0
fi

TEST_ARGUMENTS=(
    xcodebuild test-without-building
    "${COMMON_ARGUMENTS[@]}"
    -parallel-testing-enabled NO
    -maximum-concurrent-test-device-destinations 1
)
if [[ ${#ONLY_TESTING[@]} -gt 0 ]]; then
    for identifier in "${ONLY_TESTING[@]}"; do
        TEST_ARGUMENTS+=("-only-testing:$identifier")
    done
fi

run_logged test-without-building "${TEST_ARGUMENTS[@]}"
