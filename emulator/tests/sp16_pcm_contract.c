/* Fake ALSA isolates pacing from real hardware/configuration. Build twice:
 * -DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so
 * normal -> test, linked to that library; run with the ARM UI preload.
 */
#include <assert.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
typedef struct _snd_pcm snd_pcm_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;

#ifdef SP16_FAKE_ALSA
static uintptr_t sentinels[8]; /* one distinct handle per open, as real ALSA returns */
static unsigned opened;
int snd_pcm_open(snd_pcm_t **out,const char *name,int stream,int mode) {
    (void)stream; (void)mode;
    assert(!strcmp(name,"null") || !strcmp(name,"unmodeled"));
    *out=(snd_pcm_t *)&sentinels[opened++ % 8]; return 0;
}
int snd_pcm_close(snd_pcm_t *p) { (void)p; return 0; }
#define IO(name,type) snd_pcm_sframes_t name(snd_pcm_t *p,type buffer,snd_pcm_uframes_t frames) { (void)p; (void)buffer; return frames; }
IO(snd_pcm_readi,void *)
IO(snd_pcm_readn,void **)
IO(snd_pcm_writei,const void *)
IO(snd_pcm_writen,void **)
#else
int snd_pcm_open(snd_pcm_t **,const char *,int,int);
int snd_pcm_close(snd_pcm_t *);
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *,void *,snd_pcm_uframes_t);
snd_pcm_sframes_t snd_pcm_readn(snd_pcm_t *,void **,snd_pcm_uframes_t);
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *,const void *,snd_pcm_uframes_t);
snd_pcm_sframes_t snd_pcm_writen(snd_pcm_t *,void **,snd_pcm_uframes_t);
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
int main(void) {
    snd_pcm_t *handle; char buffer[1]; void *channels[]={buffer};
    assert(snd_pcm_open(&handle,"hw:ak4452audio,0",1,0)==0);
    double start=now();
    assert(snd_pcm_readi(handle,buffer,960)==960);
    assert(snd_pcm_readn(handle,channels,960)==960);
    assert(snd_pcm_writei(handle,buffer,960)==960);
    assert(snd_pcm_writen(handle,channels,960)==960);
    /* Four 960-frame transfers at the default 48 kHz require 80 ms.
     * A broad upper bound permits emulation/scheduler overhead. */
    double elapsed=now()-start;
    assert(elapsed>=.075 && elapsed<2);
    assert(snd_pcm_readi(handle,buffer,0)==0);
    assert(snd_pcm_close(handle)==0);
    /* Regression: the OEM audio thread writes three playback PCMs back to back each period. Their waits
     * must overlap (one shared board clock). 100 periods of 960 frames at 48 kHz are 2 s of real time;
     * per-call relative sleeps made this 6 s, i.e. the sequencer ran at a third of its tempo. */
    snd_pcm_t *devices[3];
    const char *names[3]={"hw:ak4458audio,0","hw:ak4452audio,0","hw:ak4452audio,1"};
    for (int i=0;i<3;i++) assert(snd_pcm_open(&devices[i],names[i],0,0)==0);
    start=now();
    for (int period=0;period<100;period++)
        for (int i=0;i<3;i++) assert(snd_pcm_writei(devices[i],buffer,960)==960);
    elapsed=now()-start;
    assert(elapsed>=1.9 && elapsed<3.2);
    for (int i=0;i<3;i++) assert(snd_pcm_close(devices[i])==0);
    assert(snd_pcm_open(&handle,"unmodeled",1,0)==0);
    assert(snd_pcm_readi(handle,buffer,960)==960);
    assert(snd_pcm_close(handle)==0);
    return 0;
}
#endif
