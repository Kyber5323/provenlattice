/* General C call-semantics fixture: direct calls, callbacks, member calls.
   Self-contained; no compilation or system headers required. */

typedef int (*callback_t)(int);

struct pipeline {
    int (*step)(int);
};

static int double_value(int value) { return value * 2; }

static int negate_value(int value) { return -value; }

int apply_callback(callback_t cb, int value) { return cb(value); }

int run_pipeline(struct pipeline *pipe, int value) {
    return pipe->step(value);
}

int run_all(int seed) {
    struct pipeline pipeline;
    pipeline.step = double_value;
    int first = apply_callback(negate_value, seed);
    int second = run_pipeline(&pipeline, first);
    return second > 0 ? second : unknown_helper(second);
}
