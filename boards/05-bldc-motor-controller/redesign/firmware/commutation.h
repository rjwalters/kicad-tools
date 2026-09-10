#ifndef COMMUTATION_H
#define COMMUTATION_H
#include <stdint.h>
/* Hall sequence 001,101,100,110,010,011; phases A=0 B=1 C=2.
 * Every valid state energizes exactly two distinct phases. */
static const uint8_t high_phase[8] = {255,0,2,2,1,0,1,255};
static const uint8_t low_phase[8]  = {255,1,0,1,2,2,0,255};
static const uint8_t next_hall[8]  = {0,5,3,1,6,4,2,0};
static inline uint8_t valid_hall(uint8_t h) { return h > 0 && h < 7; }
#endif
