/* Queue contract for /proc/udev_usb1 and /proc/udev_usbctn1 (kernel fs/proc/udev_usb.c semantics).
 * Run under the preload with SP16_USB1_SOCKET / SP16_USBCTN1_SOCKET pointing at writable paths. Messages are
 * injected the way the guest init does: one datagram per message to the node's socket path. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

static void inject(const char *path, const char *text) {
    int fd=socket(AF_UNIX,SOCK_DGRAM,0);
    struct sockaddr_un a={ .sun_family=AF_UNIX };
    strcpy(a.sun_path,path);
    assert(sendto(fd,text,strlen(text),0,(struct sockaddr *)&a,sizeof(a))==(ssize_t)strlen(text));
    close(fd);
}
static int readable(int fd) { struct pollfd p={ .fd=fd,.events=POLLIN }; return poll(&p,1,0); }
static int writable(int fd) { struct pollfd p={ .fd=fd,.events=POLLOUT }; return poll(&p,1,0)==1 && (p.revents&POLLOUT); }

int main(void) {
    const char *usb=getenv("SP16_USB1_SOCKET"), *ctn=getenv("SP16_USBCTN1_SOCKET");
    assert(usb && ctn);
    int fd=open("/proc/udev_usb1",O_RDWR);
    int cfd=open("/proc/udev_usbctn1",O_RDWR);
    assert(fd>=0 && cfd>=0 && access(usb,F_OK)==0 && access(ctn,F_OK)==0);
    assert(readable(fd)==0 && writable(fd) && writable(cfd));

    /* One message per read, in order; the node queues are independent. */
    inject(usb,"mount /media/usb1/sda1");
    inject(usb,"umount /media/usb1/sda1");
    inject(ctn,"connect");
    assert(readable(fd)==1 && readable(cfd)==1);
    char buf[64];
    ssize_t n=read(fd,buf,sizeof(buf));
    assert(n==(ssize_t)strlen("mount /media/usb1/sda1") && !memcmp(buf,"mount /media/usb1/sda1",(size_t)n));
    n=read(fd,buf,sizeof(buf));
    assert(n==(ssize_t)strlen("umount /media/usb1/sda1") && !memcmp(buf,"umount",6));
    assert(readable(fd)==0);
    n=read(cfd,buf,sizeof(buf));
    assert(n==7 && !memcmp(buf,"connect",7) && readable(cfd)==0);

    /* A short buffer truncates the message; the remainder is discarded, not left for the next read. */
    inject(usb,"mount /media/usb1/sda1");
    assert(read(fd,buf,5)==5 && !memcmp(buf,"mount",5) && readable(fd)==0);

    /* The app's own request loops back to its reader, bytes exact (no newline, no terminator). */
    const char req[]="do_umount0 /media/usb1/sda1";
    assert(write(fd,req,sizeof(req)-1)==(ssize_t)(sizeof(req)-1) && readable(fd)==1);
    n=read(fd,buf,sizeof(buf));
    assert(n==(ssize_t)(sizeof(req)-1) && !memcmp(buf,req,(size_t)n));
    assert(write(fd,"mount /media/usb1/sda1",22)==22 && read(fd,buf,sizeof(buf))==22);

    /* Requests outside the single USB slot are refused and never queued. */
    assert(write(fd,"do_umount0 /",12)==-1 && errno==EINVAL);
    assert(write(fd,"do_umount1 /media/usb1/../../etc",32)==-1 && errno==EINVAL);
    assert(write(fd,"umount /mnt/emmc",16)==-1 && errno==EINVAL);
    assert(write(fd,"connect",7)==-1 && errno==EINVAL);
    assert(readable(fd)==0);

    /* A leading '0' clears the whole queue. */
    inject(usb,"mount /media/usb1/sda1");
    inject(usb,"umount /media/usb1/sda1");
    assert(readable(fd)==1 && write(fd,"0",1)==1 && readable(fd)==0);

    /* A second opener shares the queue; closing the first keeps it alive. */
    int again=open("/proc/udev_usb1",O_RDWR);
    assert(again>=0 && again!=fd);
    inject(usb,"umount /media/usb1/sda1");
    assert(close(fd)==0 && access(usb,F_OK)==0 && readable(again)==1);
    assert(read(again,buf,sizeof(buf))==(ssize_t)strlen("umount /media/usb1/sda1"));
    assert(close(again)==0 && access(usb,F_OK)!=0 && close(cfd)==0 && access(ctn,F_OK)!=0);
    return 0;
}
