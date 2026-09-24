// Defect-shaped C++ for the semantic event layer.
//
// Every function below is wrong in one specific, named way. The point is to
// give the extractor the operations the vocabulary names -- allocate, release,
// lock, unlock, spawn -- so that acceptance can be checked against something.
// The other files in this fixture are `return N;` stubs and would exercise
// nothing but RETURN.

#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include <mutex>

namespace storage {

void *worker(void *);

static pthread_mutex_t g_index_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_log_lock = PTHREAD_MUTEX_INITIALIZER;
static std::mutex g_pool_lock;

// 4.1 memory leak: the early return skips the free.
int load_index(const char *name) {
  char *buffer = (char *)malloc(256);
  if (!buffer) {
    return -1;
  }
  if (strlen(name) > 255) {
    return -1;  // buffer is never released
  }
  free(buffer);
  return 0;
}

// 4.2 double free: both paths release the same pointer.
int release_twice(char *payload) {
  free(payload);
  if (payload) {
    free(payload);
  }
  return 0;
}

// 1.3 lock order inversion: this takes index then log, and flush_log below
// takes them the other way round.
int reindex(void) {
  pthread_mutex_lock(&g_index_lock);
  pthread_mutex_lock(&g_log_lock);
  pthread_mutex_unlock(&g_log_lock);
  pthread_mutex_unlock(&g_index_lock);
  return 0;
}

int flush_log(void) {
  pthread_mutex_lock(&g_log_lock);
  pthread_mutex_lock(&g_index_lock);
  pthread_mutex_unlock(&g_index_lock);
  pthread_mutex_unlock(&g_log_lock);
  return 0;
}

// 4.6 incomplete cleanup on the error path: the lock is still held when this
// returns.
int resize_pool(int size) {
  g_pool_lock.lock();
  char *entries = (char *)malloc(size);
  if (!entries) {
    return -1;
  }
  free(entries);
  g_pool_lock.unlock();
  return 0;
}

// RAII: the acquisition is the declaration itself, so no call in the body
// mentions it.
int snapshot(void) {
  std::lock_guard<std::mutex> guard{g_pool_lock};
  return 0;
}

// Thread started and never joined.
int start_workers(pthread_t *handle) {
  return pthread_create(handle, NULL, worker, NULL);
}

}  // namespace storage
