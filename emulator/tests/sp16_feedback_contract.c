/* Panel TX capture and CDC strip input: bounded, nonblocking, ABI-checked. */
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

struct spi_message { uint8_t multi_id, reserved, part_id, part_num; uint32_t bytes; uint64_t tx_buf; };

static int bind_datagram(const char *path) {
    int fd=socket(AF_UNIX,SOCK_DGRAM,0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,path);
    unlink(path);
    assert(fd>=0 && bind(fd,(struct sockaddr *)&address,sizeof(address))==0);
    return fd;
}

int main(void) {
    const char *tx_path=getenv("SP16_PANEL_TX"), *cdc_path=getenv("SP16_CDC_SOCKET");
    assert(tx_path && cdc_path);
    int panel=open("/dev/subucom_spi0.1",O_RDWR);
    assert(panel>=0);

    /* A frame sent before any reader exists is dropped, not queued, and never fails the OEM ioctl. */
    uint8_t frame[128];
    for (int i=0;i<128;i++) frame[i]=(uint8_t)(i*3+1);
    struct spi_message message={.multi_id=0,.part_id=0,.part_num=1,.bytes=128,.tx_buf=(uintptr_t)frame};
    assert(ioctl(panel,0x40107000,&message)==0);

    int reader=bind_datagram(tx_path);
    assert(ioctl(panel,0x40107000,&message)==0);
    uint8_t received[256];
    assert(recv(reader,received,sizeof(received),MSG_DONTWAIT)==136);
    assert(received[0]==0 && received[1]==0 && received[2]==1 && received[3]==0);
    uint32_t length; memcpy(&length,received+4,4);
    assert(length==128 && !memcmp(received+8,frame,128));
    assert(recv(reader,received,sizeof(received),MSG_DONTWAIT)==-1 && errno==EAGAIN);

    message.multi_id=2; message.part_id=1; message.part_num=3; message.bytes=16;
    assert(ioctl(panel,0x40107000,&message)==0);
    assert(recv(reader,received,sizeof(received),MSG_DONTWAIT)==24 && received[0]==2 && received[1]==1 && received[2]==3);

    /* Validation precedes capture. */
    message.bytes=129;
    assert(ioctl(panel,0x40107000,&message)==-1 && errno==EINVAL);
    message.bytes=8; message.tx_buf=0;
    assert(ioctl(panel,0x40107000,&message)==-1 && errno==EINVAL);
    assert(recv(reader,received,sizeof(received),MSG_DONTWAIT)==-1 && errno==EAGAIN);

    /* A full reader queue drops frames without blocking the OEM thread. */
    message.bytes=128; message.tx_buf=(uintptr_t)frame;
    for (int i=0;i<2000;i++) assert(ioctl(panel,0x40107000,&message)==0);

    /* CDC strip input is a 4-byte datagram that wakes epoll in timer mode. */
    int cdc=open("/dev/cdc_spi2.0",O_RDWR);
    assert(cdc>=0);
    uint8_t timer=0, state[4]={1,2,3,4};
    assert(read(cdc,state,4)==4 && !memcmp(state,"\0\0\0\0",4));
    timer=1;
    assert(ioctl(cdc,0x40017001,&timer)==0 && ioctl(cdc,0x40017006,&timer)==0);
    assert(read(cdc,state,4)==-1 && errno==EAGAIN);
    int ep=epoll_create1(EPOLL_CLOEXEC);
    struct epoll_event event={.events=EPOLLIN,.data.fd=cdc};
    assert(ep>=0 && epoll_ctl(ep,EPOLL_CTL_ADD,cdc,&event)==0);
    assert(epoll_wait(ep,&event,1,0)==0);
    int sender=socket(AF_UNIX,SOCK_DGRAM,0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,cdc_path);
    const uint8_t strip[4]={0x10,0x80,0,0};
    assert(sendto(sender,strip,4,0,(struct sockaddr *)&address,sizeof(address))==4);
    assert(epoll_wait(ep,&event,1,50)==1 && event.data.fd==cdc);
    assert(read(cdc,state,4)==4 && !memcmp(state,strip,4));
    assert(epoll_wait(ep,&event,1,0)==0);
    assert(close(cdc)==0 && access(cdc_path,F_OK)==-1 && errno==ENOENT);
    assert(close(panel)==0);
    return 0;
}
