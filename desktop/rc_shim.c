/* llvm-rc.exe shim -> `zig rc`.
 *
 * WHY: on this machine there is no Windows SDK, so neither rc.exe nor the SDK's
 * llvm-rc.exe exists, and rustup's windows-gnu toolchain ships a linker but no
 * windres. Tauri's build script (tauri-winres -> embed-resource) must hand a .rc
 * file to *some* resource compiler; it honours an `RC` / `RC_<target>` env var,
 * so we give it this 30-line forwarder instead of installing another gigabyte.
 *
 * Zig's own `zig rc` speaks the llvm-rc dialect (its help says option prefixes
 * `/`, `-`, `--` all work), so arguments pass through unchanged. MAO_ZIG points
 * at the real zig executable; falling back to plain "zig" keeps PATH-only
 * setups working too.
 *
 * Built by: zig cc -target x86_64-windows-gnu -O2 rc_shim.c -o llvm-rc.exe
 */
#include <errno.h>
#include <process.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    /* argv we hand to zig: [zig, "rc", <caller args...>, NULL] */
    char **forward = (char **)calloc((size_t)argc + 3, sizeof(char *));
    if (forward == NULL) {
        fprintf(stderr, "rc-shim: out of memory\n");
        return 2;
    }
    const char *zig = getenv("MAO_ZIG");
    if (zig == NULL || zig[0] == '\0') {
        zig = "zig";
    }
    forward[0] = (char *)zig;
    forward[1] = (char *)"rc";
    for (int i = 1; i < argc; i++) {
        forward[i + 1] = argv[i];
    }
    forward[argc + 2] = NULL;

    /* execv replaces us, so the exit code the build script sees is zig's. */
    int rc = _execv(zig, forward);
    if (rc != 0) {
        fprintf(stderr, "rc-shim: cannot exec %s (%s)\n", zig, strerror(errno));
    }
    free(forward);
    return 1;
}
