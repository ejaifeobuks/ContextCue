#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>

// Host equivalents for exercising the real sketch's synchronization.
struct k_mutex { std::mutex value; };
#define K_MUTEX_DEFINE(name) k_mutex name
#define K_FOREVER 0
inline void k_mutex_lock(k_mutex* mutex, int) { mutex->value.lock(); }
inline void k_mutex_unlock(k_mutex* mutex) { mutex->value.unlock(); }
inline std::atomic<int64_t> testUptime{1000};
inline int64_t k_uptime_get() { return testUptime.load(); }
