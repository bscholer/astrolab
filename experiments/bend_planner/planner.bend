# Whole-night planner: try every target in every half-hour slot, keep the best total.
# UNTESTED: written without a Bend install. Expect to fix syntax on first run.
# night.py appends alt_table() and gain_table() (see README) to make night.bend.

def fmax(a, b):
  if a > b:
    return a
  else:
    return b

def nth_f(xs, i):
  match xs:
    case List/Nil:
      return 0.0
    case List/Cons:
      if i == 0:
        return xs.head
      else:
        return nth_f(xs.tail, i - 1)

def nth_u(xs, i):
  match xs:
    case List/Nil:
      return 0
    case List/Cons:
      if i == 0:
        return xs.head
      else:
        return nth_u(xs.tail, i - 1)

def nth_row(xs, i):
  match xs:
    case List/Nil:
      return List/Nil
    case List/Cons:
      if i == 0:
        return xs.head
      else:
        return nth_row(xs.tail, i - 1)

def bump(xs, i):
  match xs:
    case List/Nil:
      return List/Nil
    case List/Cons:
      if i == 0:
        return List/Cons(xs.head + 1, xs.tail)
      else:
        return List/Cons(xs.head, bump(xs.tail, i - 1))

def zeros(n):
  if n == 0:
    return List/Nil
  else:
    return List/Cons(0, zeros(n - 1))

# Best total score from slot s on. `counts` is slots already given to each target.
def best(s, n_slots, n_targets, counts):
  if s == n_slots:
    return 0.0
  else:
    idle = best(s + 1, n_slots, n_targets, counts)
    return try_target(0, s, n_slots, n_targets, counts, idle)

def try_target(t, s, n_slots, n_targets, counts, acc):
  if t == n_targets:
    return acc
  else:
    a = nth_f(nth_row(alt_table(), s), t)
    if a > 0.0:
      k = nth_u(counts, t)
      v = nth_f(gain_table(), k) * a + best(s + 1, n_slots, n_targets, bump(counts, t))
      return try_target(t + 1, s, n_slots, n_targets, counts, fmax(acc, v))
    else:
      return try_target(t + 1, s, n_slots, n_targets, counts, acc)
