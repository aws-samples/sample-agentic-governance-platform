# Shared container-engine resolver for the deploy scripts.
# Sourced (not executed) by deploy-full.sh / deploy-container.sh / setup-dockerhub-auth.sh.
#
# Exposes $CONTAINER_ENGINE ("docker" or "finch"). All scripts call
# "$CONTAINER_ENGINE" instead of a hard-coded "docker" so the same flow works
# with Docker (Linux/Mac) or finch (Amazon-preferred, no Docker Desktop).
#
# Selection order:
#   1. --finch flag       -> caller sets USE_FINCH=1 before sourcing
#   2. CONTAINER_ENGINE    env var (explicit override, "docker" or "finch")
#   3. docker, if its daemon is reachable
#   4. finch, if installed
# finch's VM is started automatically when finch is selected.

resolve_container_engine() {
    if [ -n "$CONTAINER_ENGINE" ]; then
        :
    elif [ "${USE_FINCH:-0}" = "1" ]; then
        CONTAINER_ENGINE="finch"
    elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        CONTAINER_ENGINE="docker"
    elif command -v finch >/dev/null 2>&1; then
        CONTAINER_ENGINE="finch"
    else
        CONTAINER_ENGINE="docker"  # nothing usable; the check below reports it
    fi

    # finch needs its VM up before build/push/login.
    if [ "$CONTAINER_ENGINE" = "finch" ]; then
        case "$(finch vm status 2>/dev/null)" in
            *Running*)     ;;
            *Nonexistent*) echo "  Initializing finch VM..."; finch vm init >/dev/null 2>&1 ;;
            *)             echo "  Starting finch VM...";     finch vm start >/dev/null 2>&1 ;;
        esac
    fi

    if ! "$CONTAINER_ENGINE" info >/dev/null 2>&1; then
        echo "Error: container engine '$CONTAINER_ENGINE' is not running/available." >&2
        echo "       Start Docker, or pass --finch (and install finch) to use finch." >&2
        return 1
    fi

    export CONTAINER_ENGINE
}
