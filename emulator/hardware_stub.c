/* SP-16 UI-only compatibility layer. No physical hardware or audible output.
 * SPI request layouts come from the matching GPL driver contracts; unknown
 * requests fail instead of silently pretending that a device operation worked.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/mman.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define MAX_FDS 1024
enum device { NONE, PANEL, CDC, DAC, BACKLIGHT, MEDIA, PACKET };
/* media: 1 = /proc/udev_usb1, 2 = /proc/udev_usbctn1 (only meaningful when kind==MEDIA). */
struct state { enum device kind; int peer; uint32_t interval, bytes; uint16_t bits; uint8_t timer, endian, input, media; };
struct spi_message { uint8_t multi_id, reserved, part_id, part_num; uint32_t bytes; uint64_t tx_buf; };
_Static_assert(sizeof(struct spi_message)==16,"SPI transfer descriptor ABI must be 16 bytes");
static struct state devices[MAX_FDS];
static pid_t owner_pid;
static int (*original_open)(const char *, int, ...);
static int (*original_close)(int);
static ssize_t (*original_read)(int, void *, size_t);
static ssize_t (*original_write)(int, const void *, size_t);
static int (*original_ioctl)(int, unsigned long, ...);
static char panel_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
static char cdc_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
static uint64_t mono_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
}

/* Bind a native-only datagram endpoint (panel RX or CDC strip). `input` is 1 for the
 * panel and 2 for the CDC controller, which closes/unlinks its own path. */
static int input_socket(enum device kind, const char *env, const char *fallback, char *saved) {
    const char *path=getenv(env);
    if (!path) path=fallback;
    if (!*path || strlen(path)>=sizeof(panel_path)) { errno=EINVAL; return -1; }
    int fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (fd<0) return -1;
    if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,path);
    /* Refuse an existing endpoint; never unlink a socket we did not create. */
    if (bind(fd,(struct sockaddr *)&address,sizeof(address))) {
        int error=errno; original_close(fd); errno=error; return -1;
    }
    strcpy(saved,path);
    devices[fd]=(struct state){ .kind=kind,.peer=-1,.interval=3,.bytes=32,.bits=32,.input=kind==PANEL?1:2 };
    return fd;
}

/* OEM panel TX capture: each ioctl 0x40107000 frame is copied to a guest-only
 * datagram endpoint for the bridge. Nonblocking and bounded; drops are counted. */
static int tx_fd=-2;
static unsigned long tx_sent, tx_dropped;
static void tx_capture(const struct spi_message *m) {
    const char *enabled=getenv("SP16_PANEL_INPUT");
    if (!enabled || strcmp(enabled,"1") || !m->bytes) return;
    const char *path=getenv("SP16_PANEL_TX");
    if (!path) path="/dev/sp16-panel-tx";
    if (strlen(path)>=sizeof(((struct sockaddr_un *)0)->sun_path)) return;
    if (tx_fd==-2) {
        tx_fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
        if (tx_fd>=MAX_FDS) { original_close(tx_fd); tx_fd=-1; }
    }
    if (tx_fd<0) return;
    uint8_t out[8+128];
    out[0]=m->multi_id; out[1]=m->part_id; out[2]=m->part_num; out[3]=0;
    memcpy(out+4,&m->bytes,4);
    memcpy(out+8,(const void *)(uintptr_t)m->tx_buf,m->bytes);
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,path);
    if (sendto(tx_fd,out,8+m->bytes,MSG_DONTWAIT,(struct sockaddr *)&address,sizeof(address))<0) {
        if (++tx_dropped==(tx_dropped&-tx_dropped))
            fprintf(stderr,"[sp16-ui-stub] panel TX frames dropped=%lu errno=%d\n",tx_dropped,errno);
    } else tx_sent++;
}

static int idle_socket(enum device kind) {
    int pair[2];
    int flags=kind==PACKET?0:SOCK_NONBLOCK;
    if (socketpair(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC|flags,0,pair)) return -1;
    if (pair[0]>=MAX_FDS) { close(pair[0]); close(pair[1]); errno=EMFILE; return -1; }
    devices[pair[0]]=(struct state){ .kind=kind,.peer=pair[1],.interval=kind==DAC?1:3,
        .bytes=kind==DAC?16:32,.bits=kind==DAC?16:32 };
    return pair[0];
}

__attribute__((constructor)) static void initialize(void) {
    owner_pid = getpid();
    original_open = dlsym(RTLD_NEXT, "open");
    original_close = dlsym(RTLD_NEXT, "close");
    original_read = dlsym(RTLD_NEXT, "read");
    original_write = dlsym(RTLD_NEXT, "write");
    original_ioctl = dlsym(RTLD_NEXT, "ioctl");
    fprintf(stderr, "[sp16-ui-stub] enabled: simulated board devices, silent PCM\n");
}

static struct state *device(int fd) {
    return fd >= 0 && fd < MAX_FDS && devices[fd].kind ? &devices[fd] : NULL;
}

/* Media nodes (/proc/udev_usb1, /proc/udev_usbctn1) mirror the kernel's vendor FIFO message queues
 * (udev_usb.c): each node is a blocking unix datagram socket bound to a guest-local path, so one datagram is
 * one message, poll/epoll report readable iff non-empty, a read pops exactly one message (excess bytes are
 * discarded) and a read blocks while empty. Our trusted guest code injects udev-style messages by sending a
 * datagram to the path; the app's own writes loop back to the same queue and a leading '0' clears it.
 * Log the first attempts verbatim, then one count per 60 s so a polling loop cannot flood the console.
 * Counters are advisory (no lock). */
static unsigned long media_total, media_window;
static uint64_t media_report_ns;
static void media_note(const struct state *s, const char *op) {
    unsigned long total=__sync_add_and_fetch(&media_total,1);
    unsigned long window=__sync_add_and_fetch(&media_window,1);
    const char *node=s->media==2?"/proc/udev_usbctn1":"/proc/udev_usb1";
    if (total<=5) { fprintf(stderr,"[sp16-ui-stub] media %s %s (attempt %lu)\n",op,node,total); return; }
    uint64_t now=mono_ns();
    if (!media_report_ns) media_report_ns=now;
    if (now-media_report_ns>=60000000000ULL) {
        fprintf(stderr,"[sp16-ui-stub] media attempts in last 60s=%lu total=%lu last=%s %s\n",window,total,op,node);
        media_window=0; media_report_ns=now;
    }
}

static char media_path[3][sizeof(((struct sockaddr_un *)0)->sun_path)];
static int media_fd[3]={ -1,-1,-1 }, media_refs[3];

/* The app may only queue the requests it issues itself: "mount <path>", "umount <path>" and
 * "do_umount<digit> <path>", with the path under the single USB slot (no "..") because the reader thread
 * performs a real umount on it. Everything else is refused so the guest cannot be steered elsewhere. */
static int media_app_message_ok(const char *m, size_t n) {
    char text[4097];
    if (n==0 || n>=sizeof(text)) return 0;
    memcpy(text,m,n); text[n]=0;
    if (memchr(text,0,n)) return 0;
    const char *path=NULL;
    if (!strncmp(text,"mount ",6)) path=text+6;
    else if (!strncmp(text,"umount ",7)) path=text+7;
    else if (!strncmp(text,"do_umount",9) && text[9]>='0' && text[9]<='9' && text[10]==' ') path=text+11;
    if (!path || strncmp(path,"/media/usb1/",12)) return 0;
    return !strstr(path,"..");
}

static ssize_t media_write(struct state *s,int fd,const void *buffer,size_t count) {
    if (count==0) return 0;
    if (count>4096) { errno=EMSGSIZE; return -1; }
    if (*(const char *)buffer=='0') { /* kernel: leading '0' clears the whole queue */
        char sink[4096];
        while (recv(fd,sink,sizeof(sink),MSG_DONTWAIT)>=0) {}
        return (ssize_t)count;
    }
    if (!media_app_message_ok(buffer,count)) {
        fprintf(stderr,"[sp16-ui-stub] media write refused (%zu bytes, not a usb1 request)\n",count);
        errno=EINVAL; return -1;
    }
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,media_path[s->media]);
    if (sendto(fd,buffer,count,MSG_DONTWAIT,(struct sockaddr *)&address,sizeof(address))<0) {
        if (errno==EAGAIN) errno=ENOSPC;
        return -1;
    }
    return (ssize_t)count;
}

static int media_socket(int node) {
    const char *env=node==2?"SP16_USBCTN1_SOCKET":"SP16_USB1_SOCKET";
    const char *path=getenv(env);
    if (!path) path=node==2?"/dev/sp16-usbctn1":"/dev/sp16-usb1";
    if (media_fd[node]>=0) { /* every opener shares the one queue, like the kernel */
        int fd=dup(media_fd[node]);
        if (fd<0) return -1;
        if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
        media_refs[node]++;
        devices[fd]=(struct state){ .kind=MEDIA,.peer=-1,.media=(uint8_t)node };
        return fd;
    }
    if (!*path || strlen(path)>=sizeof(media_path[0])) { errno=EINVAL; return -1; }
    int fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC,0);
    if (fd<0) return -1;
    if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,path);
    /* Refuse an existing endpoint; never unlink a socket we did not create. */
    if (bind(fd,(struct sockaddr *)&address,sizeof(address))) {
        int error=errno; original_close(fd); errno=error; return -1;
    }
    strcpy(media_path[node],path);
    media_fd[node]=fd; media_refs[node]=1;
    devices[fd]=(struct state){ .kind=MEDIA,.peer=-1,.media=(uint8_t)node };
    return fd;
}

static enum device classify(const char *path) {
    if (!strcmp(path, "/dev/subucom_spi0.1")) return PANEL;
    if (!strcmp(path, "/dev/cdc_spi2.0")) return CDC;
    if (!strcmp(path, "/dev/dac_spi4.0")) return DAC;
    if (!strcmp(path, "/dev/backlight")) return BACKLIGHT;
    if (!strcmp(path, "/proc/udev_usb1") || !strcmp(path, "/proc/udev_usbctn1")) return MEDIA;
    return NONE;
}

/* Driver coverage (hardware suite HW-45, docs/architecture.md M5): a device node the shim does not serve is reported once
 * per path, so a firmware version that uses another driver is diagnosable; the open itself is passed through. */
static int unknown_device_node(const char *path) {
    static const char *standard[]={"/dev/null","/dev/full","/dev/zero","/dev/random","/dev/urandom","/dev/tty",
                                   "/dev/console","/dev/ptmx",0};
    if (!strncmp(path,"/proc/udev_",11)) return 1;
    if (strncmp(path,"/dev/",5) || !strncmp(path,"/dev/sp16-",10) || !strncmp(path,"/dev/shm/",9)
        || !strncmp(path,"/dev/pts/",9)) return 0;
    for (int i=0; standard[i]; i++) if (!strcmp(path,standard[i])) return 0;
    return 1;
}

static void note_unknown_device(const char *path) {
    static char seen[16][64];
    static int count;
    for (int i=0; i<count; i++) if (!strncmp(seen[i],path,sizeof seen[i]-1)) return;
    if (count<16) { strncpy(seen[count],path,sizeof seen[count]-1); count++; }
    fprintf(stderr, "[sp16-ui-stub] unknown device open path=%s\n", path);
}

static int open_device(const char *path, int flags, mode_t mode) {
    if (!original_open) original_open=dlsym(RTLD_NEXT,"open");
    if (!original_close) original_close=dlsym(RTLD_NEXT,"close");
    enum device kind = classify(path);
    if (!kind) {
        if (path && unknown_device_node(path)) note_unknown_device(path);
        return original_open(path, flags, mode);
    }
    /* Idle sockets can participate in epoll without an always-readable EOF. */
    const char *enabled=getenv("SP16_PANEL_INPUT");
    int native=enabled && !strcmp(enabled,"1");
    int fd=-1;
    if (native && kind==PANEL) fd=input_socket(PANEL,"SP16_PANEL_SOCKET","/dev/sp16-panel-input",panel_path);
    else if (native && kind==CDC) fd=input_socket(CDC,"SP16_CDC_SOCKET","/dev/sp16-cdc-input",cdc_path);
    else if (kind==MEDIA) {
        int node=!strcmp(path,"/proc/udev_usbctn1")?2:1;
        fd=media_socket(node);
        if (fd<0) { /* no queue socket available: keep the old inert behaviour rather than failing the open */
            fd=idle_socket(kind);
            if (fd>=0) devices[fd].media=(uint8_t)node;
        }
    }
    else fd=idle_socket(kind);
    fprintf(stderr, "[sp16-ui-stub] open %s => fd=%d\n", path, fd);
    return fd;
}

int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list args; va_start(args, flags); mode = va_arg(args, mode_t); va_end(args); }
    return open_device(path, flags, mode);
}
int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list args; va_start(args, flags); mode = va_arg(args, mode_t); va_end(args); }
    return open_device(path, flags, mode);
}

int close(int fd) {
    if (!original_close) original_close=dlsym(RTLD_NEXT,"close");
    struct state *s = device(fd);
    if (s) {
        int peer=s->peer;
        if (s->input==1) { unlink(panel_path); panel_path[0]=0; }
        else if (s->input==2) { unlink(cdc_path); cdc_path[0]=0; }
        else if (s->kind==MEDIA && s->peer<0 && s->media && media_fd[s->media]>=0) {
            if (--media_refs[s->media]<=0) {
                unlink(media_path[s->media]); media_path[s->media][0]=0;
                media_fd[s->media]=-1;
            } else if (fd==media_fd[s->media]) {
                /* The bound descriptor closes first: keep the queue alive through a duplicate. */
                int keep=dup(fd);
                if (keep>=0 && keep<MAX_FDS) { devices[keep]=(struct state){ .kind=MEDIA,.peer=-1,.media=s->media }; media_fd[s->media]=keep; }
                else media_fd[s->media]=-1;
            }
        }
        memset(s, 0, sizeof(*s));
        if (peer>=0) original_close(peer);
    }
    return original_close(fd);
}

ssize_t write(int fd, const void *buffer, size_t count) {
    if (!original_write) original_write=dlsym(RTLD_NEXT,"write");
    struct state *s = device(fd);
    if (!s) return original_write(fd, buffer, count);
    if (s->kind==MEDIA) {
        media_note(s,"write");
        if (s->peer<0) return media_write(s,fd,buffer,count);
    }
    if (s->kind == MEDIA || s->kind==PACKET) { errno=ENOTSUP; return -1; }
    if (s->timer) { errno=EFAULT; return -1; }
    /* The OEM synchronous SPI write contract returns zero, not count. */
    return 0;
}

ssize_t read(int fd, void *buffer, size_t count) {
    if (!original_read) original_read=dlsym(RTLD_NEXT,"read");
    struct state *s = device(fd);
    if (!s) return original_read(fd, buffer, count);
    /* CDC reads with the scan timer off keep the earlier neutral-state contract. */
    if (s->input==2 && !s->timer) {
        if (count>128) { errno=EMSGSIZE; return -1; }
        memset(buffer,0,count);
        return (ssize_t)count;
    }
    if (s->input) {
        if (count>128) { errno=EMSGSIZE; return -1; }
        ssize_t result=original_read(fd,buffer,count);
        static unsigned logged=0;
        if (s->input==1 && result==128 && logged++<8) {
            const uint8_t *frame=buffer;
            fprintf(stderr,"[sp16-ui-stub] panel RX bytes=128 id=%u buttons=0x%02x selector=%02x%02x crc=%02x%02x\n",
                    frame[0],frame[1],frame[60],frame[61],frame[124],frame[125]);
        }
        return result;
    }
    if (s->kind==MEDIA) {
        media_note(s,"read");
        if (s->peer<0) return original_read(fd,buffer,count); /* blocks while empty, pops one message */
    }
    if (s->kind==MEDIA || s->kind==PACKET || s->timer) { errno=EAGAIN; return -1; }
    if (count > 128) { errno=EMSGSIZE; return -1; }
    memset(buffer, 0, count); /* neutral touch/control state, not sampled hardware */
    return (ssize_t)count;
}

int ioctl(int fd, unsigned long request, ...) {
    if (!original_ioctl) original_ioctl=dlsym(RTLD_NEXT,"ioctl");
    va_list args; va_start(args, request); void *arg=va_arg(args, void *); va_end(args);
    struct state *s = device(fd);
    if (!s) return original_ioctl(fd, request, arg);
    if (s->kind==BACKLIGHT && request==0x40047000UL) return arg?0:(errno=EFAULT,-1);
    if (s->kind!=PANEL && s->kind!=CDC && s->kind!=DAC) { errno=ENOTTY; return -1; }
    if (!arg) { errno=EFAULT; return -1; }
    switch (request) {
    case 0x40107000UL: {
        struct spi_message *m=arg;
        if (m->bytes>128 || (m->bytes && !m->tx_buf)) { errno=EINVAL; return -1; }
        /* Accept the validated TX configuration; no device response is invented. */
        if (s->kind==PANEL) tx_capture(m);
        return 0;
    }
    case 0x80017001UL: *(uint8_t *)arg=s->timer; return 0;
    case 0x40017001UL: s->timer=!!*(uint8_t *)arg; return 0;
    case 0x80047002UL: *(uint32_t *)arg=s->interval; return 0;
    case 0x40047002UL: s->interval=*(uint32_t *)arg; return 0;
    case 0x80027003UL: *(uint16_t *)arg=s->bits; return 0;
    case 0x40027003UL:
        if (s->timer) { errno=EBUSY; return -1; } s->bits=*(uint16_t *)arg; return 0;
    case 0x80047004UL: *(uint32_t *)arg=s->bytes; return 0;
    case 0x40047004UL:
        if (s->timer) { errno=EBUSY; return -1; }
        if (*(uint32_t *)arg>128) { errno=EINVAL; return -1; } s->bytes=*(uint32_t *)arg; return 0;
    case 0x40017005UL: if (s->kind==PANEL) return 0; break; /* dummy TX */
    case 0x40047007UL: if (s->kind==PANEL) return 0; break; /* ready pin selector */
    case 0x80017006UL: if (s->kind==PANEL) { *(uint8_t *)arg=1; return 0; } break;
    case 0x40017008UL: if (s->kind==PANEL) { *(uint8_t *)arg=s->endian; return 0; } break;
    case 0x40017009UL: if (s->kind==PANEL) { s->endian=*(uint8_t *)arg; return 0; } break;
    case 0x40017006UL: if (s->kind==CDC) return 0; break; /* capacitive converter reset */
    }
    fprintf(stderr, "[sp16-ui-stub] unsupported ioctl fd=%d request=0x%lx\n", fd, request);
    errno=ENOTTY; return -1;
}

/* Use the stock ALSA null plugin only for the three observed board PCM ports.
 * Keep real PCM handles/parameter validation, with pacing to avoid a busy loop.
 */
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;
/* Diagnostic counters (logged every few seconds) so audio pacing can be measured, not guessed. */
struct ax_ring;
struct pcm_state { snd_pcm_t *handle; unsigned int rate; int stream; char name[24];
                   uint64_t calls, frames, slept_ns, reported_ns, deadline_ns; int tid;
                   uint32_t logged; uint8_t io_logged; struct pcm_tap *tap; struct ax_ring *ax; };
static struct pcm_state pcms[16];
/* Research-only PCM tap (SP16_PCM_TAP=<dir>, set by guest-init only for --trace pcmtap builds). Playback frames
 * written by the OEM are copied with one memcpy into a preallocated, bounded, MAP_SHARED file
 * <dir>/sp16-pcm-<device>.raw, so the audio thread makes no syscall and never waits: pacing is untouched.
 * Capture starts at the first non-zero frame (SP16_PCM_TAP_ARM=0: immediately) and stops after
 * SP16_PCM_TAP_SECONDS (default 3, max 30). The 64-byte little-endian header is finalized last (done=1). */
#define PCM_TAP_HDR 64u
#define PCM_TAP_DEFAULT_S 3u
#define PCM_TAP_MAX_S 30u
struct pcm_tap { uint8_t *map; size_t cap_frames; uint32_t bpf, armed, is_float, arm_signal; uint64_t seen; };
static void tap_u32(uint8_t *h,unsigned off,uint32_t v) { memcpy(h+off,&v,4); }
static void tap_u64(uint8_t *h,unsigned off,uint64_t v) { memcpy(h+off,&v,8); }
static uint64_t tap_get64(const uint8_t *h,unsigned off) { uint64_t v; memcpy(&v,h+off,8); return v; }
static void pcm_state_init_tap(struct pcm_state *s,unsigned channels,int format);
static void pcm_tap_close(struct pcm_state *s);
static void ax_init(struct pcm_state *s,unsigned channels,int format);
static void ax_close(struct pcm_state *s);
static void ax_feed(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames);
static void ax_report(struct pcm_state *s);
static struct pcm_state *pcm(snd_pcm_t *handle) {
    for (unsigned i=0;i<16;i++) if (pcms[i].handle==handle) return &pcms[i];
    return NULL;
}
/* Each hw_params call is logged once per handle (bit per call site). */
static int first_time(struct pcm_state *s, unsigned bit) {
    if (s->logged & (1u<<bit)) return 0;
    s->logged |= 1u<<bit; return 1;
}
/* Resolve a real libasound symbol without caching a stale pointer; NULL when libasound is absent. */
static void *real_symbol(const char *name) { return dlsym(RTLD_NEXT,name); }
/* libasound's hw_params entry points carry the ALSA_0.9.0rc4 version; the default lookup can resolve a different
 * ABI (observed as garbage return values), so use the versioned symbol and fall back to the plain name. */
static void *alsa_symbol(const char *name) {
    void *symbol=dlvsym(RTLD_NEXT,name,"ALSA_0.9.0rc4");
    return symbol?symbol:dlsym(RTLD_NEXT,name);
}
static const char *format_label(int format) {
    const char *(*name)(int)=real_symbol("snd_pcm_format_name");
    const char *text=name?name(format):NULL;
    return text?text:"?";
}
static const char *access_label(int access) {
    const char *(*name)(int)=real_symbol("snd_pcm_access_name");
    const char *text=name?name(access):NULL;
    return text?text:"?";
}
int snd_pcm_open(snd_pcm_t **handle, const char *name, int stream, int mode) {
    int (*real)(snd_pcm_t **,const char *,int,int)=dlsym(RTLD_NEXT,"snd_pcm_open");
    int matched=!strcmp(name,"hw:ak4452audio,0") || !strcmp(name,"hw:ak4452audio,1") || !strcmp(name,"hw:ak4458audio,0");
    int result=real(handle,matched?"null":name,stream,mode);
    if (matched) {
        fprintf(stderr,"[sp16-ui-stub] PCM %s -> null stream=%d result=%d\n",name,stream,result);
        if (!result) for (unsigned i=0;i<16;i++) if (!pcms[i].handle) {
            pcms[i]=(struct pcm_state){.handle=*handle,.rate=48000,.stream=stream};
            snprintf(pcms[i].name,sizeof(pcms[i].name),"%s",name); break; }
    }
    return result;
}
int snd_pcm_close(snd_pcm_t *handle) {
    int (*real)(snd_pcm_t *)=dlsym(RTLD_NEXT,"snd_pcm_close");
    struct pcm_state *s=pcm(handle); if (s) { pcm_tap_close(s); ax_close(s); memset(s,0,sizeof(*s)); }
    return real(handle);
}
/* hw_params wrappers: forward every call unchanged; log only handles in pcms[] (the redirected board PCMs),
 * once per call site. Values are read before and after the real call so requested and returned match. */
int snd_pcm_hw_params_set_access(snd_pcm_t *handle,snd_pcm_hw_params_t *params,int access) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,int)=alsa_symbol("snd_pcm_hw_params_set_access");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params,access);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,0)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_access requested=%d(%s) result=%d\n",
                                      s->name,access,access_label(access),result);
    return result;
}
int snd_pcm_hw_params_set_format(snd_pcm_t *handle,snd_pcm_hw_params_t *params,int format) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,int)=alsa_symbol("snd_pcm_hw_params_set_format");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params,format);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,1)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_format requested=%d(%s) result=%d\n",
                                      s->name,format,format_label(format),result);
    return result;
}
int snd_pcm_hw_params_set_channels(snd_pcm_t *handle,snd_pcm_hw_params_t *params,unsigned int channels) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned int)=alsa_symbol("snd_pcm_hw_params_set_channels");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params,channels);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,2)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_channels requested=%u result=%d\n",
                                      s->name,channels,result);
    return result;
}
int snd_pcm_hw_params_set_rate(snd_pcm_t *handle,snd_pcm_hw_params_t *params,unsigned int rate,int direction) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned int,int)=alsa_symbol("snd_pcm_hw_params_set_rate");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params,rate,direction);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,3)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_rate requested=%u dir=%d result=%d\n",
                                      s->name,rate,direction,result);
    return result;
}
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *handle,snd_pcm_hw_params_t *params,unsigned int *rate,int *direction) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned int *,int *)=dlvsym(RTLD_NEXT,"snd_pcm_hw_params_set_rate_near","ALSA_0.9.0rc4");
    if (!real) real=alsa_symbol("snd_pcm_hw_params_set_rate_near");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    unsigned int requested=rate?*rate:0; int requested_dir=direction?*direction:0;
    int result=real(handle,params,rate,direction);
    struct pcm_state *s=pcm(handle); if (!result && s && rate && *rate) s->rate=*rate;
    if (s && first_time(s,4)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_rate_near requested=%u dir=%d returned=%u dir=%d result=%d\n",
                                      s->name,requested,requested_dir,rate?*rate:0,direction?*direction:0,result);
    return result;
}
int snd_pcm_hw_params_set_period_size_near(snd_pcm_t *handle,snd_pcm_hw_params_t *params,snd_pcm_uframes_t *frames,int *direction) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,snd_pcm_uframes_t *,int *)=alsa_symbol("snd_pcm_hw_params_set_period_size_near");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    snd_pcm_uframes_t requested=frames?*frames:0; int requested_dir=direction?*direction:0;
    int result=real(handle,params,frames,direction);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,5)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_period_size_near requested=%lu dir=%d returned=%lu dir=%d result=%d\n",
                                      s->name,(unsigned long)requested,requested_dir,(unsigned long)(frames?*frames:0),direction?*direction:0,result);
    return result;
}
int snd_pcm_hw_params_set_buffer_size_near(snd_pcm_t *handle,snd_pcm_hw_params_t *params,snd_pcm_uframes_t *frames) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,snd_pcm_uframes_t *)=alsa_symbol("snd_pcm_hw_params_set_buffer_size_near");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    snd_pcm_uframes_t requested=frames?*frames:0;
    int result=real(handle,params,frames);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,6)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_buffer_size_near requested=%lu returned=%lu result=%d\n",
                                      s->name,(unsigned long)requested,(unsigned long)(frames?*frames:0),result);
    return result;
}
/* Commit: after a successful snd_pcm_hw_params the params object holds the installed configuration, so the
 * negotiated values are read back with the real getters (not interposed). */
int snd_pcm_hw_params(snd_pcm_t *handle,snd_pcm_hw_params_t *params) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *)=alsa_symbol("snd_pcm_hw_params");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,8)) {
        int access=-1, format=-1, dir=0; unsigned int channels=0, rate=0, periods=0; snd_pcm_uframes_t period=0, buffer=0;
        int (*get_access)(const snd_pcm_hw_params_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_access");
        int (*get_format)(const snd_pcm_hw_params_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_format");
        int (*get_channels)(const snd_pcm_hw_params_t *,unsigned int *)=alsa_symbol("snd_pcm_hw_params_get_channels");
        int (*get_rate)(const snd_pcm_hw_params_t *,unsigned int *,int *)=alsa_symbol("snd_pcm_hw_params_get_rate");
        int (*get_period)(const snd_pcm_hw_params_t *,snd_pcm_uframes_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_period_size");
        int (*get_buffer)(const snd_pcm_hw_params_t *,snd_pcm_uframes_t *)=alsa_symbol("snd_pcm_hw_params_get_buffer_size");
        int (*get_periods)(const snd_pcm_hw_params_t *,unsigned int *,int *)=alsa_symbol("snd_pcm_hw_params_get_periods");
        if (get_access) get_access(params,&access);
        if (get_format) get_format(params,&format);
        if (get_channels) get_channels(params,&channels);
        if (get_rate) get_rate(params,&rate,&dir);
        if (get_period) get_period(params,&period,&dir);
        if (get_buffer) get_buffer(params,&buffer);
        if (get_periods) get_periods(params,&periods,&dir);
        fprintf(stderr,"[sp16-ui-stub] PCM %s hw_params result=%d access=%s(%d) format=%s(%d) channels=%u rate=%u period_frames=%lu buffer_frames=%lu periods=%u stream=%s\n",
                s->name,result,access_label(access),access,format_label(format),format,channels,rate,
                (unsigned long)period,(unsigned long)buffer,periods,s->stream?"capture":"playback");
        if (!result) { pcm_state_init_tap(s,channels,format); ax_init(s,channels,format); }
    }
    return result;
}
/* Pace each stream against an absolute deadline on the (shared) 48 kHz board clock. A relative sleep per
 * call made one audio thread that serves several devices (the OEM engine writes three playback PCMs back to
 * back) advance at 1/N of real time, so the sequencer ran about 3.5x slow. With per-stream deadlines the
 * waits overlap, as they do on the real I2S clock. A stream that falls too far behind is resynchronised
 * instead of bursting to catch up. */
#define PCM_MAX_LAG_NS 100000000ULL
#define PCM_STATS_NS 60000000000ULL

/* Header layout: 0 magic "SP16PCM1", 8 version, 12 done, 16 channels, 20 alsa format, 24 rate, 28 bytes/frame,
 * 32 cap_frames, 40 captured_frames, 48 first_frame_index (frames written before capture started),
 * 56 start_mono_ns. */
static unsigned tap_seconds(void) {
    const char *text=getenv("SP16_PCM_TAP_SECONDS"); long v=text?strtol(text,NULL,10):PCM_TAP_DEFAULT_S;
    return v<1?PCM_TAP_DEFAULT_S:(v>(long)PCM_TAP_MAX_S?PCM_TAP_MAX_S:(unsigned)v);
}
static unsigned tap_width(int format) { /* bytes per sample: S16_LE=2, FLOAT_LE=14 -> 4, S32_LE=10 -> 4 */
    return format==2?2:(format==14||format==10?4:0);
}
static void pcm_state_init_tap(struct pcm_state *s,unsigned channels,int format) {
    const char *dir=getenv("SP16_PCM_TAP");
    if (!dir || !*dir || s->stream!=0 || s->tap) return;
    unsigned width=tap_width(format);
    if (!channels || !width || channels>32) return;
    static struct pcm_tap taps[16]; struct pcm_tap *t=&taps[s-pcms];
    memset(t,0,sizeof(*t));
    t->is_float=format==14; t->bpf=channels*width; t->cap_frames=(size_t)tap_seconds()*s->rate;
    const char *dev=strchr(s->name,':'); dev=dev?dev+1:s->name;
    char path[256], clean[48]; unsigned n=0;
    for (;dev[n] && n<sizeof(clean)-1;n++) clean[n]=(dev[n]>='a'&&dev[n]<='z')||(dev[n]>='0'&&dev[n]<='9')?dev[n]:'-';
    clean[n]=0;
    snprintf(path,sizeof(path),"%s/sp16-pcm-%s.raw",dir,clean);
    size_t bytes=PCM_TAP_HDR+t->cap_frames*t->bpf;
    int fd=open(path,O_RDWR|O_CREAT|O_TRUNC,0600);
    if (fd<0) { fprintf(stderr,"[sp16-ui-stub] PCM tap %s open failed errno=%d\n",path,errno); return; }
    if (ftruncate(fd,(off_t)bytes)!=0) { close(fd); return; }
    void *map=mmap(NULL,bytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); close(fd);
    if (map==MAP_FAILED) return;
    t->map=map; t->armed=getenv("SP16_PCM_TAP_ARM")&&!strcmp(getenv("SP16_PCM_TAP_ARM"),"0"); t->arm_signal=!t->armed;
    memcpy(t->map,"SP16PCM1",8); tap_u32(t->map,8,1); tap_u32(t->map,16,channels); tap_u32(t->map,20,(uint32_t)format);
    tap_u32(t->map,24,s->rate); tap_u32(t->map,28,t->bpf); tap_u64(t->map,32,t->cap_frames);
    s->tap=t;
    fprintf(stderr,"[sp16-ui-stub] PCM tap %s -> %s cap_frames=%lu bytes=%lu arm_on_signal=%d\n",
            s->name,path,(unsigned long)t->cap_frames,(unsigned long)bytes,!t->armed);
}
static void pcm_tap_close(struct pcm_state *s) {
    if (!s->tap) return;
    struct pcm_tap *t=s->tap;
    if (!t->map[12]) tap_u32(t->map,12,1);
    munmap(t->map,PCM_TAP_HDR+t->cap_frames*t->bpf); t->map=NULL; s->tap=NULL;
}
/* Called after the real write with the data the OEM just queued. Bounded and non-blocking: no syscall, no lock. */
static void pcm_tap_feed(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames) {
    struct pcm_tap *t=s->tap;
    /* The consumer re-arms a finished capture by zeroing captured/start and then done: wait for fresh signal. */
    if (t && t->map && t->arm_signal && t->armed && !t->map[12] && !tap_get64(t->map,40)) t->armed=0;
    if (!t || !t->map || frames<=0 || !buffer || tap_get64(t->map,40)>=t->cap_frames || (t->map[12])) {
        if (t) t->seen+=frames>0?(uint64_t)frames:0;
        return;
    }
    size_t bytes=(size_t)frames*t->bpf; const uint8_t *src=buffer;
    if (!t->armed) {
        size_t i=0;
        if (t->is_float) { /* -0.0 (sign bit only) is silence */
            for (;i+4<=bytes;i+=4) { uint32_t w; memcpy(&w,src+i,4); if (w<<1) break; }
            if (i+4>bytes) i=bytes;
        } else for (;i<bytes && !src[i];i++) {}
        if (i==bytes) { t->seen+=(uint64_t)frames; return; }
        t->armed=1; tap_u64(t->map,48,t->seen); tap_u64(t->map,56,mono_ns());
        /* start at the frame holding the first non-zero byte so silence does not lead the capture */
        size_t skip=i/t->bpf; src+=skip*t->bpf; bytes-=skip*t->bpf; t->seen+=skip; frames-=(snd_pcm_sframes_t)skip;
        tap_u64(t->map,48,t->seen);
    } else if (!tap_get64(t->map,56)) { tap_u64(t->map,48,t->seen); tap_u64(t->map,56,mono_ns()); }
    uint64_t have=tap_get64(t->map,40), room=t->cap_frames-have;
    uint64_t take=(uint64_t)frames<room?(uint64_t)frames:room;
    memcpy(t->map+PCM_TAP_HDR+have*t->bpf,src,take*t->bpf);
    tap_u64(t->map,40,have+take); t->seen+=(uint64_t)frames;
    if (have+take>=t->cap_frames) tap_u32(t->map,12,2); /* done=2: full */
}

/* Audio transport (M4b1, off unless SP16_PCM_PORT names the guest virtio-serial node, set by guest-init for
 * sp16.audio=file). The audio thread (snd_pcm_writei) only copies the interesting channels into a per-stream
 * lock-free SPSC ring of 256 period slots (about 370 ms): no lock, no syscall besides the clock read pace() already makes, and it
 * never waits. A full ring drops the new period and counts it (the frame index still advances, so the host sees a
 * gap). A forwarder thread (the only consumer) polls every 2 ms, coalesces contiguous slots into one framed
 * packet and writes it to the port; if the host is slow or absent only the ring fills and drops. The deadline
 * clock in pace() stays the master. The host can ping (16 bytes "SPNG", pad, u64 host ns); the pong lets it
 * measure the clock offset and so the guest-write-to-host-receipt latency.
 * Packet (little endian, 48-byte header): 0 magic "SPCM"; 4 type (1 config, 2 data, 4 pong);
 * 5 stream (0 ak4458,0; 1 ak4452,0; 2 ak4452,1); 6 ALSA format; 7 sent channels; 8 total channels; 9 first channel;
 * 10 reserved; 12 rate; 16 sequence (global, one counter for the port); 20 frames; 24 guest ns of the oldest frame
 * (pong: now); 32 absolute frame index of the first frame (pong: echoed host ns); 40 cumulative dropped frames;
 * 44 payload bytes; then the payload (interleaved sent channels). */
#define AX_SLOTS 256u
#define AX_SLOT_FRAMES 128u
#define AX_SLOT_BYTES (AX_SLOT_FRAMES*32u)
#define AX_STREAMS 3u
#define AX_HDR 48u
#define AX_MAX_SLOTS_PER_PACKET 16u
struct ax_slot { uint64_t ts_ns, frame_index; uint32_t frames; uint8_t data[AX_SLOT_BYTES]; };
struct ax_ring { struct ax_slot slots[AX_SLOTS]; uint32_t head, tail, cfg_gen, active;
                 uint32_t rate, width, total_ch, first_ch, sent_ch, fmt, stream, high_water;
                 uint64_t next_frame, drops, packets, frames_sent; };
static struct ax_ring ax_rings[AX_STREAMS];
static const char *ax_port_path;
static int ax_thread_started;
static void *ax_forwarder(void *unused);
static void ax_put32(uint8_t *h,unsigned o,uint32_t v) { memcpy(h+o,&v,4); }
static void ax_put64(uint8_t *h,unsigned o,uint64_t v) { memcpy(h+o,&v,8); }
static void ax_init(struct pcm_state *s,unsigned channels,int format) {
    ax_port_path=getenv("SP16_PCM_PORT");
    if (!ax_port_path || !*ax_port_path || s->stream!=0 || s->ax || !channels || channels>16) return;
    unsigned id=!strcmp(s->name,"hw:ak4458audio,0")?0:!strcmp(s->name,"hw:ak4452audio,0")?1:!strcmp(s->name,"hw:ak4452audio,1")?2:AX_STREAMS;
    unsigned width=tap_width(format);
    if (id>=AX_STREAMS || !width || width*channels>32) return;
    struct ax_ring *r=&ax_rings[id];
    unsigned first=0, count=channels;
    if (id==0 && channels==16) { first=12; count=4; } /* ch12/14 carry the metronome and samples; ch13/15 are not audio-like */
    const char *env=id==0?getenv("SP16_PCM_PORT_CHANNELS"):NULL; /* research override "first:count" */
    if (env) { unsigned a,b; if (sscanf(env,"%u:%u",&a,&b)==2 && b && a+b<=channels) { first=a; count=b; } }
    __atomic_store_n(&r->active,0,__ATOMIC_RELEASE);
    r->rate=s->rate; r->width=width; r->total_ch=channels; r->first_ch=first; r->sent_ch=count; r->fmt=(uint32_t)format; r->stream=id;
    __atomic_add_fetch(&r->cfg_gen,1,__ATOMIC_RELEASE);
    __atomic_store_n(&r->active,1,__ATOMIC_RELEASE);
    s->ax=r;
    fprintf(stderr,"[sp16-ui-stub] audio transport %s -> %s stream=%u fmt=%d channels=%u..%u of %u rate=%u\n",
            s->name,ax_port_path,id,format,first,first+count-1,channels,s->rate);
    if (!ax_thread_started) {
        int (*create)(unsigned long *,const void *,void *(*)(void *),void *)=dlsym(RTLD_DEFAULT,"pthread_create");
        unsigned long tid;
        if (create && !create(&tid,NULL,ax_forwarder,NULL)) ax_thread_started=1;
        else fprintf(stderr,"[sp16-ui-stub] audio transport: no forwarder thread (pthread_create unavailable)\n");
    }
}
static void ax_close(struct pcm_state *s) {
    if (!s->ax) return;
    __atomic_store_n(&s->ax->active,0,__ATOMIC_RELEASE); s->ax=NULL;
}
static void ax_feed(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames) {
    struct ax_ring *r=s->ax;
    if (!r || frames<=0 || !buffer) return;
    uint64_t ts=mono_ns();
    const uint8_t *src=buffer; unsigned bpf=r->width*r->total_ch, run=r->width*r->sent_ch, skip=r->width*r->first_ch;
    while (frames>0) {
        uint32_t n=frames>(snd_pcm_sframes_t)AX_SLOT_FRAMES?AX_SLOT_FRAMES:(uint32_t)frames;
        uint32_t head=r->head, tail=__atomic_load_n(&r->tail,__ATOMIC_ACQUIRE);
        if (head-tail>=AX_SLOTS) __atomic_add_fetch(&r->drops,n,__ATOMIC_RELAXED);
        else {
            struct ax_slot *slot=&r->slots[head%AX_SLOTS];
            slot->ts_ns=ts; slot->frame_index=r->next_frame; slot->frames=n;
            if (run==bpf) memcpy(slot->data,src,(size_t)n*bpf);
            else for (uint32_t i=0;i<n;i++) memcpy(slot->data+(size_t)i*run,src+(size_t)i*bpf+skip,run);
            if (head+1-tail>r->high_water) r->high_water=head+1-tail;
            __atomic_store_n(&r->head,head+1,__ATOMIC_RELEASE);
        }
        r->next_frame+=n; src+=(size_t)n*bpf; frames-=(snd_pcm_sframes_t)n;
    }
}
static void ax_report(struct pcm_state *s) {
    struct ax_ring *r=s->ax;
    fprintf(stderr,"[sp16-ui-stub] audio-tx %s packets=%lu frames_sent=%lu drops=%lu ring_high_water=%u/%u\n",s->name,
            (unsigned long)__atomic_load_n(&r->packets,__ATOMIC_RELAXED),(unsigned long)__atomic_load_n(&r->frames_sent,__ATOMIC_RELAXED),
            (unsigned long)__atomic_load_n(&r->drops,__ATOMIC_RELAXED),r->high_water,AX_SLOTS);
}
static uint8_t ax_out[AX_HDR+AX_MAX_SLOTS_PER_PACKET*AX_SLOT_BYTES];
static uint32_t ax_seq, ax_cfg_sent[AX_STREAMS];
static int ax_fd=-1;
static int ax_send(const uint8_t *data,size_t size) {
    size_t done=0;
    while (done<size) {
        ssize_t n=write(ax_fd,data+done,size-done);
        if (n>0) { done+=(size_t)n; continue; }
        if (n<0 && errno==EINTR) continue;
        if (n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) { struct pollfd p={ .fd=ax_fd,.events=POLLOUT }; poll(&p,1,5); continue; }
        close(ax_fd); ax_fd=-1; return -1; /* port went away: reopen; a half packet is discarded */
    }
    return 0;
}
static void ax_header(uint8_t *h,unsigned type,struct ax_ring *r,uint32_t frames,uint64_t ns,uint64_t index,uint32_t payload) {
    memset(h,0,AX_HDR); memcpy(h,"SPCM",4); h[4]=(uint8_t)type;
    if (r) { h[5]=(uint8_t)r->stream; h[6]=(uint8_t)r->fmt; h[7]=(uint8_t)r->sent_ch; h[8]=(uint8_t)r->total_ch; h[9]=(uint8_t)r->first_ch;
             ax_put32(h,12,r->rate); ax_put32(h,40,(uint32_t)__atomic_load_n(&r->drops,__ATOMIC_RELAXED)); }
    ax_put32(h,16,ax_seq++); ax_put32(h,20,frames); ax_put64(h,24,ns); ax_put64(h,32,index); ax_put32(h,44,payload);
}
static void *ax_forwarder(void *unused) {
    (void)unused;
    uint8_t ping[16]; unsigned have=0;
    for (;;) {
        if (ax_fd<0) {
            ax_fd=open(ax_port_path,O_RDWR|O_NONBLOCK|O_CLOEXEC);
            if (ax_fd<0) { struct timespec t={ .tv_sec=1 }; nanosleep(&t,NULL); continue; }
            have=0; memset(ax_cfg_sent,0,sizeof ax_cfg_sent);
        }
        ssize_t got=read(ax_fd,ping+have,sizeof ping-have);
        if (got>0) have+=(unsigned)got;
        if (have==sizeof ping) {
            have=0;
            if (!memcmp(ping,"SPNG",4)) { uint8_t pong[AX_HDR]; uint64_t t0; memcpy(&t0,ping+8,8);
                ax_header(pong,4,NULL,0,mono_ns(),t0,0); if (ax_send(pong,sizeof pong)) continue; }
        }
        for (unsigned id=0;id<AX_STREAMS && ax_fd>=0;id++) {
            struct ax_ring *r=&ax_rings[id];
            uint32_t gen=__atomic_load_n(&r->cfg_gen,__ATOMIC_ACQUIRE);
            if (!gen) continue;
            if (gen!=ax_cfg_sent[id]) { uint8_t cfg[AX_HDR]; ax_header(cfg,1,r,0,mono_ns(),r->next_frame,0);
                if (ax_send(cfg,sizeof cfg)) break; ax_cfg_sent[id]=gen; }
            uint32_t tail=r->tail, head=__atomic_load_n(&r->head,__ATOMIC_ACQUIRE);
            while (tail!=head && ax_fd>=0) {
                uint32_t count=0, frames=0; uint64_t expect=r->slots[tail%AX_SLOTS].frame_index;
                size_t bytes=0; uint64_t first_ns=r->slots[tail%AX_SLOTS].ts_ns, first_index=expect;
                while (tail+count!=head && count<AX_MAX_SLOTS_PER_PACKET) {
                    struct ax_slot *slot=&r->slots[(tail+count)%AX_SLOTS];
                    if (slot->frame_index!=expect) break; /* a drop: the next packet carries the new frame index */
                    size_t size=(size_t)slot->frames*r->width*r->sent_ch;
                    memcpy(ax_out+AX_HDR+bytes,slot->data,size); bytes+=size; frames+=slot->frames; expect+=slot->frames; count++;
                }
                ax_header(ax_out,2,r,frames,first_ns,first_index,(uint32_t)bytes);
                __atomic_store_n(&r->tail,tail+count,__ATOMIC_RELEASE); tail+=count;
                if (ax_send(ax_out,AX_HDR+bytes)) break;
                __atomic_add_fetch(&r->packets,1,__ATOMIC_RELAXED); __atomic_add_fetch(&r->frames_sent,frames,__ATOMIC_RELAXED);
            }
        }
        struct timespec nap={ .tv_nsec=2000000 }; nanosleep(&nap,NULL);
    }
    return NULL;
}
static const char *const io_names[4]={"writei","writen","readi","readn"};
static void pace(snd_pcm_t *handle,snd_pcm_sframes_t frames,unsigned method) {
    struct pcm_state *s=pcm(handle);
    if (!s) return;
    if (!(s->io_logged & (1u<<method))) {
        s->io_logged |= 1u<<method;
        fprintf(stderr,"[sp16-ui-stub] PCM %s io=%s first_call frames=%ld stream=%s\n",
                s->name,io_names[method],(long)frames,s->stream?"capture":"playback");
    }
    if (frames<=0) return;
    uint64_t step=(uint64_t)frames*1000000000ULL/s->rate, now=mono_ns();
    if (!s->deadline_ns || now>s->deadline_ns+PCM_MAX_LAG_NS) s->deadline_ns=now;
    s->deadline_ns+=step;
    uint64_t slept=0;
    if (s->deadline_ns>now) {
        struct timespec until={ .tv_sec=(time_t)(s->deadline_ns/1000000000ULL), .tv_nsec=(long)(s->deadline_ns%1000000000ULL) };
        while (clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&until,NULL)==EINTR) {}
        slept=mono_ns()-now;
    }
    uint64_t after=now+slept;
    s->calls++; s->frames+=(uint64_t)frames; s->slept_ns+=slept; s->tid=(int)syscall(SYS_gettid);
    if (!s->reported_ns) s->reported_ns=after;
    if (after-s->reported_ns>=PCM_STATS_NS) {
        double secs=(double)(after-s->reported_ns)/1e9;
        double rate=(double)s->frames/secs;
        fprintf(stderr,"[sp16-ui-stub] audio %s %s tid=%d rate=%u frames/s=%.0f speed=%.2fx calls/s=%.1f sleep_share=%.2f\n",
                s->name,s->stream?"capture":"playback",s->tid,s->rate,rate,rate/(double)s->rate,(double)s->calls/secs,
                (double)s->slept_ns/1e9/secs);
        if (s->ax) ax_report(s);
        s->calls=s->frames=s->slept_ns=0; s->reported_ns=after;
    }
}
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *handle,const void *buffer,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,const void *,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_writei");
    snd_pcm_sframes_t result=real(handle,buffer,frames);
    struct pcm_state *tapped=pcm(handle);
    if (tapped && tapped->tap) pcm_tap_feed(tapped,buffer,result);
    if (tapped && tapped->ax) ax_feed(tapped,buffer,result);
    pace(handle,result,0); return result;
}
snd_pcm_sframes_t snd_pcm_writen(snd_pcm_t *handle,void **buffers,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,void **,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_writen");
    snd_pcm_sframes_t result=real(handle,buffers,frames); pace(handle,result,1); return result;
}
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *handle,void *buffer,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,void *,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_readi");
    snd_pcm_sframes_t result=real(handle,buffer,frames); pace(handle,result,2); return result;
}
snd_pcm_sframes_t snd_pcm_readn(snd_pcm_t *handle,void **buffers,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,void **,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_readn");
    snd_pcm_sframes_t result=real(handle,buffers,frames); pace(handle,result,3); return result;
}

/* The UI harness has no init/service stack. Refuse guest shell helpers,
 * including the application's crash-handler format/mount/recovery commands.
 */
/* Log the refused path and at most 8 argv entries (each cut to 40 bytes, the line to ~200 bytes). Logging only. */
static void log_blocked_exec(const char *path,char *const argv[]) {
    char args[192]="";
    size_t used=0;
    unsigned count=0;
    for (unsigned i=0; argv && argv[i] && i<8; i++, count++) {
        int n=snprintf(args+used,sizeof args-used,"%s%.40s",i?" ":"",argv[i]);
        if (n<0 || (size_t)n>=sizeof args-used) { used=sizeof args-1; break; }
        used+=(size_t)n;
    }
    while (argv && argv[count]) count++;
    fprintf(stderr,"[sp16-ui-stub] blocked guest helper exec: %.120s argc=%u argv=[%s]\n",path,count,args);
}

/* Exec policy. Exactly one command is allowed: sync, with an optional -n, either executed directly
 * (path /bin/sync or /usr/bin/sync, argv[0] "sync" or the path) or as the whole command string of
 * `sh -c` (argv = {sh, -c, "sync"} and the same spellings with -n or a full path). Anything else,
 * including format, mount, mkfs, reboot, crash-handler and recovery scripts, stays refused with ENOTSUP.
 * The OEM shell runner forks before exec (it waits on the child), so an allowed sync runs only in a
 * forked child: it calls sync(2) and _exit(0), which the parent observes as a successful child. In the
 * application process itself the call is refused, because exiting there would end the app. */
static int is_sync_path(const char *s) {
    return s && (!strcmp(s,"sync") || !strcmp(s,"/bin/sync") || !strcmp(s,"/usr/bin/sync"));
}
static int sync_allowed(const char *path,char *const argv[]) {
    if (!path || !argv || !argv[0]) return 0;
    if (is_sync_path(path) && (!strcmp(argv[0],"sync") || !strcmp(argv[0],path))) {
        return !argv[1] || (!strcmp(argv[1],"-n") && !argv[2]);
    }
    if ((!strcmp(path,"/bin/sh") || !strcmp(path,"sh")) && argv[1] && argv[2] && !argv[3] && !strcmp(argv[1],"-c")) {
        static const char *const forms[] = {"sync","sync -n","/bin/sync","/bin/sync -n","/usr/bin/sync","/usr/bin/sync -n"};
        for (unsigned i=0; i<sizeof forms/sizeof forms[0]; i++) if (!strcmp(argv[2],forms[i])) return 1;
    }
    return 0;
}
static int exec_policy(const char *path,char *const argv[],int complete) {
    if (complete && getpid()!=owner_pid && sync_allowed(path,argv)) {
        fprintf(stderr,"[sp16-ui-stub] allowed sync no-op exec: %.120s\n",path);
        sync();
        _exit(0);
    }
    log_blocked_exec(path,argv);
    errno=ENOTSUP; return -1;
}
int execl(const char *path,const char *arg,...) {
    /* Gather the NULL-terminated variadic list. complete=0 when it exceeds the 8 slots, which the policy refuses. */
    char *argv[9]; unsigned n=0;
    va_list args; va_start(args,arg);
    const char *a=arg;
    for (; a && n<8; a=va_arg(args,const char *)) argv[n++]=(char *)a;
    va_end(args);
    argv[n]=NULL;
    return exec_policy(path,argv,a==NULL);
}
int execv(const char *path,char *const argv[]) {
    return exec_policy(path,argv,1);
}
int execve(const char *path,char *const argv[],char *const envp[]) {
    (void)envp; return execv(path,argv);
}

/* Media nodes are only observable here through poll(); epoll/ppoll are not interposed. */
int poll(struct pollfd *fds,nfds_t count,int timeout) {
    int (*real)(struct pollfd *,nfds_t,int)=dlsym(RTLD_NEXT,"poll");
    for (nfds_t i=0; fds && i<count; i++) {
        struct state *s=device(fds[i].fd);
        if (s && s->kind==MEDIA) media_note(s,"poll");
    }
    return real(fds,count,timeout);
}

int socket(int domain,int type,int protocol) {
    int (*real)(int,int,int)=dlsym(RTLD_NEXT,"socket");
    if (domain==AF_PACKET && (type&0xf)==SOCK_DGRAM && protocol==1544) {
        int fd=idle_socket(PACKET);
        fprintf(stderr,"[sp16-ui-stub] ARP socket -> isolated idle fd=%d\n",fd);
        return fd;
    }
    return real(domain,type,protocol);
}
int bind(int fd,const struct sockaddr *address,socklen_t length) {
    int (*real)(int,const struct sockaddr *,socklen_t)=dlsym(RTLD_NEXT,"bind");
    struct state *s=device(fd);
    if (s && s->kind==PACKET && address && address->sa_family==AF_PACKET) return 0;
    return real(fd,address,length);
}
ssize_t sendto(int fd,const void *buffer,size_t length,int flags,const struct sockaddr *address,socklen_t address_length) {
    ssize_t (*real)(int,const void *,size_t,int,const struct sockaddr *,socklen_t)=dlsym(RTLD_NEXT,"sendto");
    struct state *s=device(fd);
    if (s && s->kind==PACKET) return (ssize_t)length; /* discarded; never sent on a network */
    return real(fd,buffer,length,flags,address,address_length);
}
