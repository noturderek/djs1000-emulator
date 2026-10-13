/* Native panel packets must wake epoll and survive the timer-on read path. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int main(void) {
    const char *path=getenv("SP16_PANEL_SOCKET");
    assert(path && strlen(path)<sizeof(((struct sockaddr_un *)0)->sun_path));
    int fd=open("/dev/subucom_spi0.1",O_RDWR);
    assert(fd>=0);
    assert(open("/dev/subucom_spi0.1",O_RDWR)==-1 && errno==EADDRINUSE);
    int ep=epoll_create1(EPOLL_CLOEXEC);
    struct epoll_event event={.events=EPOLLIN,.data.fd=fd};
    assert(ep>=0 && epoll_ctl(ep,EPOLL_CTL_ADD,fd,&event)==0);
    assert(epoll_wait(ep,&event,1,0)==0);
    uint8_t frame[129]={1}, received[129]={0}, timer=1;
    frame[60]=0xff; frame[61]=0xff; frame[124]=0x31; frame[125]=0x78;
    assert(ioctl(fd,0x40017001,&timer)==0);
    assert(read(fd,received,128)==-1 && errno==EAGAIN);
    int sender=socket(AF_UNIX,SOCK_DGRAM,0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,path);
    assert(sender>=0 && sendto(sender,frame,128,0,(struct sockaddr *)&address,sizeof(address))==128);
    assert(epoll_wait(ep,&event,1,50)==1 && event.data.fd==fd);
    assert(read(fd,received,129)==-1 && errno==EMSGSIZE);
    assert(read(fd,received,128)==128 && !memcmp(frame,received,128));
    assert(epoll_wait(ep,&event,1,0)==0);
    assert(close(fd)==0 && access(path,F_OK)==-1 && errno==ENOENT);
    assert(close(sender)==0 && close(ep)==0);
    return 0;
}
