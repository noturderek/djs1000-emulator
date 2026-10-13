/* Exec policy contract for hardware_stub.c. Only a sync no-op may succeed; every other exec is refused.
 * Each attempt that must run in a child uses fork(), as the OEM shell runner does: a successful sync
 * ends the child with status 0 (sync(2) then _exit(0)); a refused exec returns -1 with ENOTSUP.
 * The direct call in this process must be refused, because an allowed sync never returns to the app.
 * arm-contracts.sh greps the stub log for the allowed and blocked lines this test produces.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

/* Runs one exec in a forked child. The child exits 0 when the exec was refused with ENOTSUP and 3 when it
 * returned anything else; a successful sync never returns here and shows up as the child's own exit 0. */
static int refused_in_child(int (*attempt)(void)) {
    pid_t pid=fork();
    assert(pid>=0);
    if (pid==0) {
        errno=0;
        int result=attempt();
        _exit(result==-1 && errno==ENOTSUP ? 0 : 3);
    }
    int status=0;
    assert(waitpid(pid,&status,0)==pid);
    return WIFEXITED(status) && WEXITSTATUS(status)==0;
}
static int allowed_in_child(int (*attempt)(void)) {
    pid_t pid=fork();
    assert(pid>=0);
    if (pid==0) {
        int result=attempt();
        _exit(result==-1 ? 4 : 5); /* reaching here means the exec was refused: never a pass */
    }
    int status=0;
    assert(waitpid(pid,&status,0)==pid);
    return WIFEXITED(status) && WEXITSTATUS(status)==0;
}

static int sync_direct(void) { return execl("/bin/sync","sync","-n",(char *)0); }
static int sync_bare(void) { return execl("/usr/bin/sync","sync",(char *)0); }
static int sync_execve(void) {
    char *argv[]={"sync","-n",NULL}; char *envp[]={NULL};
    return execve("/bin/sync",argv,envp);
}
static int sync_shell(void) { return execl("/bin/sh","sh","-c","sync -n",(char *)0); }
static int sync_shell_bare(void) { return execl("/bin/sh","sh","-c","/bin/sync",(char *)0); }
static int mkfs(void) { return execl("/sbin/mkfs.ext4","mkfs.ext4","/dev/sda1",(char *)0); }
static int mount_data(void) { return execl("/bin/mount","mount","/dev/sda1","/root/pdj",(char *)0); }
static int umount_data(void) { return execl("/bin/umount","umount","/root/pdj",(char *)0); }
static int format_script(void) { return execl("/bin/sh","sh","-c","/sbin/format.sh",(char *)0); }
static int chained(void) { return execl("/bin/sh","sh","-c","sync; mkfs.ext4 /dev/sda1",(char *)0); }
static int sync_with_format(void) { return execl("/bin/sh","sh","-c","sync -n /dev/sda1",(char *)0); }
static int sync_extra_arg(void) { return execl("/bin/sync","sync","-n","-f",(char *)0); }
static int sync_nine_args(void) {
    return execl("/bin/sync","sync","-n","a","b","c","d","e","f","g",(char *)0);
}
static int reboot_cmd(void) { return execl("/sbin/reboot","reboot",(char *)0); }
static int unknown_cmd(void) { return execl("/bin/unknown-helper","unknown-helper",(char *)0); }
static int sh_prohibited(void) { return execl("/bin/sh","sh","-c","echo prohibited",(char *)0); }

int main(void) {
    /* Direct calls in the application process are refused, even for sync. */
    errno=0;
    assert(execl("/bin/sync","sync","-n",(char *)0)==-1 && errno==ENOTSUP);

    /* Allowed: sync no-op success, in forked children, through every accepted spelling. */
    assert(allowed_in_child(sync_direct));
    assert(allowed_in_child(sync_bare));
    assert(allowed_in_child(sync_execve));
    assert(allowed_in_child(sync_shell));
    assert(allowed_in_child(sync_shell_bare));

    /* Refused: format, mount, mkfs, reboot, format scripts, chained or extended commands, unknown helpers. */
    assert(refused_in_child(mkfs));
    assert(refused_in_child(mount_data));
    assert(refused_in_child(umount_data));
    assert(refused_in_child(format_script));
    assert(refused_in_child(chained));
    assert(refused_in_child(sync_with_format));
    assert(refused_in_child(sync_extra_arg));
    assert(refused_in_child(sync_nine_args));
    assert(refused_in_child(reboot_cmd));
    assert(refused_in_child(unknown_cmd));
    assert(refused_in_child(sh_prohibited));

    printf("exec contract passed\n");
    return 0;
}
