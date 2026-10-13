/* Contract for the research-only PCM tap. Build twice like sp16_hwparams_contract.c
 * (-DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so; normal -> test). Run with the ARM UI preload and
 *   SP16_PCM_TAP=/tmp/run SP16_PCM_TAP_SECONDS=1   (tap on)   or without SP16_PCM_TAP and argv[1]="off".
 * Asserts: leading silence is skipped, content is bit-exact, capture stops at the bound (1 s) and the file never
 * grows, the done flag is set, capture streams are not tapped, tap off creates nothing, and writes stay paced
 * on the 44.1 kHz clock.
 */
#include <assert.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;

#ifdef SP16_FAKE_ALSA
static uintptr_t sentinels[4]; static unsigned opened, stored_channels, stored_rate; static int stored_format;
int snd_pcm_open(snd_pcm_t **out,const char *name,int stream,int mode) {
    (void)stream; (void)mode; assert(!strcmp(name,"null")); *out=(snd_pcm_t *)&sentinels[opened++%4]; return 0; }
int snd_pcm_close(snd_pcm_t *p) { (void)p; return 0; }
int snd_pcm_hw_params_set_format(snd_pcm_t *p,snd_pcm_hw_params_t *q,int f) { (void)p; (void)q; stored_format=f; return 0; }
int snd_pcm_hw_params_set_channels(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned c) { (void)p; (void)q; stored_channels=c; return 0; }
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)p; (void)q; (void)d; stored_rate=*r; return 0; }
int snd_pcm_hw_params(snd_pcm_t *p,snd_pcm_hw_params_t *q) { (void)p; (void)q; return 0; }
int snd_pcm_hw_params_get_format(const snd_pcm_hw_params_t *q,int *f) { (void)q; *f=stored_format; return 0; }
int snd_pcm_hw_params_get_channels(const snd_pcm_hw_params_t *q,unsigned *c) { (void)q; *c=stored_channels; return 0; }
int snd_pcm_hw_params_get_rate(const snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)q; *r=stored_rate; *d=0; return 0; }
const char *snd_pcm_format_name(int f) { return f==2 ? "S16_LE" : "OTHER"; }
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *p,const void *b,snd_pcm_uframes_t n) { (void)p; (void)b; return n; }
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *p,void *b,snd_pcm_uframes_t n) { (void)p; (void)b; return n; }
#else
int snd_pcm_open(snd_pcm_t **,const char *,int,int);
int snd_pcm_close(snd_pcm_t *);
int snd_pcm_hw_params_set_format(snd_pcm_t *,snd_pcm_hw_params_t *,int);
int snd_pcm_hw_params_set_channels(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned);
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned *,int *);
int snd_pcm_hw_params(snd_pcm_t *,snd_pcm_hw_params_t *);
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *,const void *,snd_pcm_uframes_t);
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static void configure(snd_pcm_t *h,int format,unsigned channels) {
    char params[64]; snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params; unsigned rate=44100; int dir=0;
    assert(snd_pcm_hw_params_set_format(h,p,format)==0);
    assert(snd_pcm_hw_params_set_channels(h,p,channels)==0);
    assert(snd_pcm_hw_params_set_rate_near(h,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params(h,p)==0);
}
int main(int argc,char **argv) {
    int tap_on=!(argc>1 && !strcmp(argv[1],"off"));
    snd_pcm_t *h, *cap; enum { CH=16, FRAMES=64 };
    int16_t silence[FRAMES*CH]={0}, signal[FRAMES*CH];
    for (int i=0;i<FRAMES*CH;i++) signal[i]=(int16_t)(i%251+1);   /* never zero */
    assert(snd_pcm_open(&h,"hw:ak4458audio,0",0,0)==0);
    configure(h,2,CH);
    assert(snd_pcm_open(&cap,"hw:ak4452audio,0",1,0)==0);        /* capture stream: must not be tapped */
    configure(cap,14,2);
    double start=now();
    for (int i=0;i<10;i++) assert(snd_pcm_writei(h,silence,FRAMES)==FRAMES);          /* leading silence */
    for (int i=0;i<1000;i++) assert(snd_pcm_writei(h,signal,FRAMES)==FRAMES);         /* 64000 frames = 1.45 s */
    double elapsed=now()-start;
    assert(elapsed>=1.4 && elapsed<3.5);                              /* 64640 frames paced at 44.1 kHz */
    assert(snd_pcm_close(cap)==0);
    assert(snd_pcm_close(h)==0);
    {   /* float stream: negative zero is silence and must not arm the tap; 0.5 does */
        snd_pcm_t *f; uint32_t neg[128], half[128];
        for (int i=0;i<128;i++) { neg[i]=0x80000000u; half[i]=0x3f000000u; }
        assert(snd_pcm_open(&f,"hw:ak4452audio,1",0,0)==0);
        configure(f,14,2);
        assert(snd_pcm_writei(f,neg,64)==64);
        assert(snd_pcm_close(f)==0);
        if (tap_on) {
            int nfd=open("/tmp/run/sp16-pcm-ak4452audio-1.raw",O_RDONLY); uint8_t nh[64]; uint64_t got;
            assert(nfd>=0 && read(nfd,nh,64)==64); memcpy(&got,nh+40,8); assert(got==0); close(nfd);
        }
        assert(snd_pcm_open(&f,"hw:ak4452audio,1",0,0)==0);
        configure(f,14,2);
        assert(snd_pcm_writei(f,neg,64)==64 && snd_pcm_writei(f,half,64)==64);
        assert(snd_pcm_close(f)==0);
        if (tap_on) {
            int nfd=open("/tmp/run/sp16-pcm-ak4452audio-1.raw",O_RDONLY); uint8_t nh[64]; uint64_t got;
            assert(nfd>=0 && read(nfd,nh,64)==64); memcpy(&got,nh+40,8); assert(got==64);
            memcpy(&got,nh+48,8); assert(got==64); close(nfd);
        }
    }
    int fd=open("/tmp/run/sp16-pcm-ak4458audio-0.raw",O_RDONLY);
    if (!tap_on) { assert(fd<0); return 0; }
    assert(fd>=0);
    assert(access("/tmp/run/sp16-pcm-ak4452audio-0.raw",F_OK)!=0);
    uint8_t hdr[64]; assert(read(fd,hdr,64)==64);
    uint32_t v32; uint64_t v64;
    assert(!memcmp(hdr,"SP16PCM1",8));
    memcpy(&v32,hdr+12,4); assert(v32==2);                          /* done: full */
    memcpy(&v32,hdr+16,4); assert(v32==CH);
    memcpy(&v32,hdr+24,4); assert(v32==44100);
    memcpy(&v32,hdr+28,4); assert(v32==CH*2);
    memcpy(&v64,hdr+32,8); assert(v64==44100);                       /* 1 s bound */
    memcpy(&v64,hdr+40,8); assert(v64==44100);                       /* never exceeds the bound */
    memcpy(&v64,hdr+48,8); assert(v64==10*FRAMES);                   /* silence skipped, index recorded */
    off_t size=lseek(fd,0,SEEK_END); assert(size==64+44100*CH*2);   /* file never grows */
    lseek(fd,64,SEEK_SET);
    int16_t first[FRAMES*CH]; assert(read(fd,first,sizeof(first))==(ssize_t)sizeof(first));
    assert(!memcmp(first,signal,sizeof(first)));                     /* bit exact from the first non-zero frame */
    return 0;
}
#endif
