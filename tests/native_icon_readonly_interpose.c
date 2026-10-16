/* Test-only observation: block write-capable probes before the kernel/TCC.
 * The runner also applies Seatbelt deny file-write* and deny network*.
 * No system authorization is requested or changed by this library.
 */
#include <sys/attr.h>
#include <sys/xattr.h>
#include <unistd.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>
#include <execinfo.h>

#define INTERPOSE(replacement, replacee) \
__attribute__((used)) static struct { const void *replacement; const void *replacee; } \
_interpose_##replacee __attribute__((section("__DATA,__interpose"))) = \
{ (const void *)&replacement, (const void *)&replacee };

static _Atomic unsigned trace_count;

static int watched(const char *path) {
    const char *root = getenv("ICON_FIXTURE_ROOT");
    if (!path || !root) return 0;
    size_t length = strlen(root);
    return strncmp(path, root, length) == 0 && (path[length] == '/' || path[length] == '\0');
}

static void note(const char *operation, const char *path, unsigned long argument) {
    if (!watched(path)) return;
    unsigned count = atomic_fetch_add(&trace_count, 1);
    if (count < 256) dprintf(2, "TRACE %s arg=0x%lx path=%s\n", operation, argument, path);
    else if (count == 256) dprintf(2, "TRACE_LIMIT\n");
}

static const char *fd_path(int fd, char *buffer) {
    return fcntl(fd, F_GETPATH, buffer) == 0 ? buffer : NULL;
}

static const char *at_path(int fd, const char *path, char *buffer) {
    if (!path || path[0] == '/') return path;
    char parent[PATH_MAX];
    if (fd == AT_FDCWD) {
        if (!getcwd(parent, sizeof(parent))) return path;
    } else if (!fd_path(fd, parent)) return path;
    snprintf(buffer, PATH_MAX, "%s/%s", parent, path);
    return buffer;
}

static int blocked_user_access(const char *path, void *attributes) {
    struct attrlist *list = attributes;
    if (!(list->commonattr & ATTR_CMN_USERACCESS)) return 0;
    note("BLOCK_USERACCESS", path, list->commonattr);
    if (watched(path) && getenv("ICON_TRACE_STACK")) {
        void *frames[20];
        backtrace_symbols_fd(frames, backtrace(frames, 20), 2);
    }
    errno = EACCES;
    return 1;
}

__attribute__((constructor)) static void installed(void) {
    dprintf(2, "TRACE interpose-loaded\n");
}

static int observed_getattrlist(const char *path, void *attrs, void *buffer, size_t size, unsigned long options) {
    note("getattrlist", path, ((struct attrlist *)attrs)->commonattr);
    if (blocked_user_access(path, attrs)) return -1;
    return getattrlist(path, attrs, buffer, size, options);
}
INTERPOSE(observed_getattrlist, getattrlist)

static int observed_getattrlistat(int fd, const char *path, void *attrs, void *buffer, size_t size, unsigned long options) {
    char absolute[PATH_MAX]; const char *name = at_path(fd, path, absolute);
    note("getattrlistat", name, ((struct attrlist *)attrs)->commonattr);
    if (blocked_user_access(name, attrs)) return -1;
    return getattrlistat(fd, path, attrs, buffer, size, options);
}
INTERPOSE(observed_getattrlistat, getattrlistat)

static int observed_fgetattrlist(int fd, void *attrs, void *buffer, size_t size, unsigned long options) {
    char path[PATH_MAX]; const char *name = fd_path(fd, path);
    note("fgetattrlist", name, ((struct attrlist *)attrs)->commonattr);
    if (blocked_user_access(name, attrs)) return -1;
    return fgetattrlist(fd, attrs, buffer, size, options);
}
INTERPOSE(observed_fgetattrlist, fgetattrlist)

static int observed_access(const char *path, int mode) {
    note("access", path, mode);
    if (mode & W_OK) { note("BLOCK_WRITE_ACCESS", path, mode); errno = EACCES; return -1; }
    return access(path, mode);
}
INTERPOSE(observed_access, access)

static int observed_faccessat(int fd, const char *path, int mode, int flags) {
    char absolute[PATH_MAX]; const char *name = at_path(fd, path, absolute);
    note("faccessat", name, mode);
    if (mode & W_OK) { note("BLOCK_WRITE_ACCESS", name, mode); errno = EACCES; return -1; }
    return faccessat(fd, path, mode, flags);
}
INTERPOSE(observed_faccessat, faccessat)

static int observed_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
    note("open", path, flags);
    if ((flags & O_ACCMODE) != O_RDONLY || flags & (O_CREAT | O_TRUNC | O_APPEND)) {
        note("BLOCK_WRITE_OPEN", path, flags); errno = EACCES; return -1;
    }
    return open(path, flags, mode);
}
INTERPOSE(observed_open, open)

static int observed_openat(int fd, const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, int); va_end(ap); }
    char absolute[PATH_MAX]; const char *name = at_path(fd, path, absolute);
    note("openat", name, flags);
    if ((flags & O_ACCMODE) != O_RDONLY || flags & (O_CREAT | O_TRUNC | O_APPEND)) {
        note("BLOCK_WRITE_OPEN", name, flags); errno = EACCES; return -1;
    }
    return openat(fd, path, flags, mode);
}
INTERPOSE(observed_openat, openat)

static int observed_setattrlist(const char *path, void *attrs, void *buffer, size_t size, unsigned long options) {
    (void)buffer; (void)size; (void)options;
    note("BLOCK_SETATTR", path, ((struct attrlist *)attrs)->commonattr); errno = EACCES; return -1;
}
INTERPOSE(observed_setattrlist, setattrlist)

static int observed_setxattr(const char *path, const char *name, const void *value, size_t size, u_int32_t position, int options) {
    (void)name; (void)value; (void)size; (void)position;
    note("BLOCK_SETXATTR", path, options); errno = EACCES; return -1;
}
INTERPOSE(observed_setxattr, setxattr)
