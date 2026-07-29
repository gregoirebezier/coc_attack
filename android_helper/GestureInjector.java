package coc.farm2;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.MotionEvent;
import java.io.BufferedReader;
import java.io.FileReader;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * High-fidelity MotionEvent injection for:
 *
 * <pre>
 * path     x1 y1 t1 x2 y2 t2 ...   (t = ms from finger-down; last t is duration)
 * timeline n1 x y t ... [n2 ...]   (1-10 fingers; times from group start)
 * multi    n1 x y t ... n2 ...     (alias of timeline; 1-10 fingers)
 * dual     …                       (alias of multi with exactly two fingers)
 * session  &lt;file&gt;                 (full take: lines "t_ms finger_id x y phase")
 * pinch    x1s y1s x1e y1e x2s y2s x2e y2e durationMs  (legacy)
 * </pre>
 *
 * {@code session} is the fast path for recorded macros: one JVM for the whole
 * take, including inter-group gaps (slept on-device).
 */
public final class GestureInjector {
    private static final int INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH = 2;
    private static final int PINCH_MAX_FRAMES = 64;
    private static final int PINCH_FRAME_INTERVAL_MS = 16;
    /** Two-handed deploy chords; matches Android's typical pointer ceiling. */
    private static final int MAX_FINGERS = 10;

    private final Object inputManager;
    private final Method injectInputEvent;

    private GestureInjector() throws ReflectiveOperationException {
        Class<?> inputManagerClass =
                Class.forName("android.hardware.input.InputManagerGlobal");
        Method getInstance = inputManagerClass.getDeclaredMethod("getInstance");
        getInstance.setAccessible(true);
        inputManager = getInstance.invoke(null);
        injectInputEvent =
                inputManagerClass.getDeclaredMethod(
                        "injectInputEvent", InputEvent.class, int.class);
        injectInputEvent.setAccessible(true);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException("usage: path|pinch|multi|dual ...");
        }
        String mode = args[0];
        if ("path".equals(mode)) {
            if (args.length < 4 || ((args.length - 1) % 3) != 0) {
                throw new IllegalArgumentException("path x1 y1 t1 [x2 y2 t2 ...]");
            }
            int pointCount = (args.length - 1) / 3;
            int[] xs = new int[pointCount];
            int[] ys = new int[pointCount];
            int[] ts = new int[pointCount];
            for (int i = 0; i < pointCount; i++) {
                xs[i] = Integer.parseInt(args[1 + i * 3]);
                ys[i] = Integer.parseInt(args[2 + i * 3]);
                ts[i] = Integer.parseInt(args[3 + i * 3]);
            }
            new GestureInjector().pathTimed(xs, ys, ts);
            return;
        }
        if ("pinch".equals(mode)) {
            if (args.length != 10) {
                throw new IllegalArgumentException(
                        "pinch x1s y1s x1e y1e x2s y2s x2e y2e durationMs");
            }
            int[] values = new int[9];
            for (int i = 0; i < 9; i++) {
                values[i] = Integer.parseInt(args[i + 1]);
            }
            if (values[8] <= 0) {
                throw new IllegalArgumentException("durationMs must be positive");
            }
            if (values[0] == values[4] && values[1] == values[5]) {
                throw new IllegalArgumentException(
                        "pinch pointers must start at different points");
            }
            new GestureInjector()
                    .pinch(
                            values[0],
                            values[1],
                            values[2],
                            values[3],
                            values[4],
                            values[5],
                            values[6],
                            values[7],
                            values[8]);
            return;
        }
        if ("timeline".equals(mode) || "multi".equals(mode) || "dual".equals(mode)) {
            List<int[][]> paths = parseMultiPaths(args);
            if ("dual".equals(mode) && paths.size() != 2) {
                throw new IllegalArgumentException("dual requires exactly two paths");
            }
            if (paths.isEmpty() || paths.size() > MAX_FINGERS) {
                throw new IllegalArgumentException(
                        "timeline/multi requires 1-" + MAX_FINGERS + " finger paths");
            }
            new GestureInjector().multiTimed(paths);
            return;
        }
        if ("session".equals(mode)) {
            if (args.length != 2) {
                throw new IllegalArgumentException("session <events-file>");
            }
            new GestureInjector().sessionFromFile(args[1]);
            return;
        }
        throw new IllegalArgumentException("unknown mode: " + mode);
    }

    /**
     * Replay a full recorded take from a text file.
     *
     * <p>Each line: {@code t_ms finger_id x y phase} with phase in
     * {@code down|move|up}. Times are absolute from session start. Fingers may
     * go up and down again across groups; gaps are slept on-device.
     */
    private void sessionFromFile(String path) throws Exception {
        List<int[]> events = new ArrayList<>();
        try (BufferedReader reader = new BufferedReader(new FileReader(path))) {
            String line;
            int lineNo = 0;
            while ((line = reader.readLine()) != null) {
                lineNo++;
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) {
                    continue;
                }
                String[] parts = line.split("\\s+");
                if (parts.length != 5) {
                    throw new IllegalArgumentException(
                            "session line " + lineNo + " expected: t finger x y phase");
                }
                int t = Integer.parseInt(parts[0]);
                int finger = Integer.parseInt(parts[1]);
                int x = Integer.parseInt(parts[2]);
                int y = Integer.parseInt(parts[3]);
                int phase = parsePhase(parts[4]);
                if (t < 0) {
                    throw new IllegalArgumentException("session times cannot be negative");
                }
                if (finger < 0 || finger >= MAX_FINGERS) {
                    throw new IllegalArgumentException(
                            "session finger_id must be 0-" + (MAX_FINGERS - 1));
                }
                events.add(new int[] {t, finger, x, y, phase});
            }
        }
        if (events.isEmpty()) {
            throw new IllegalArgumentException("session file has no events");
        }
        sessionTimed(events);
    }

    private static int parsePhase(String phase) {
        if ("down".equals(phase) || "0".equals(phase)) {
            return 0;
        }
        if ("move".equals(phase) || "1".equals(phase)) {
            return 1;
        }
        if ("up".equals(phase) || "2".equals(phase)) {
            return 2;
        }
        throw new IllegalArgumentException("unknown session phase: " + phase);
    }

    private void sessionTimed(List<int[]> events) throws Exception {
        // Sort by time, then phase (down before move before up at same t).
        Collections.sort(
                events,
                (a, b) -> {
                    if (a[0] != b[0]) {
                        return Integer.compare(a[0], b[0]);
                    }
                    return Integer.compare(a[4], b[4]);
                });

        boolean[] down = new boolean[MAX_FINGERS];
        int[] curX = new int[MAX_FINGERS];
        int[] curY = new int[MAX_FINGERS];
        List<Integer> active = new ArrayList<>();
        MotionEvent.PointerProperties[] properties = pointerProperties(MAX_FINGERS);
        MotionEvent.PointerCoords[] coordinates = pointerCoordinates(MAX_FINGERS);

        // Session clock origin (sleeps) vs per-gesture MotionEvent downTime.
        // Reusing one downTime across many taps/holds makes Android/CoC treat
        // later bouts as one giant gesture — replay diverges hard from the take.
        long sessionStart = SystemClock.uptimeMillis();
        long gestureDownTime = sessionStart;
        try {
            int index = 0;
            while (index < events.size()) {
                int t = events.get(index)[0];
                sleepUntil(sessionStart, t);

                // Apply all events at this timestamp.
                while (index < events.size() && events.get(index)[0] == t) {
                    int[] ev = events.get(index++);
                    int finger = ev[1];
                    int x = ev[2];
                    int y = ev[3];
                    int phase = ev[4];
                    curX[finger] = x;
                    curY[finger] = y;

                    if (phase == 0) { // down
                        if (down[finger]) {
                            throw new IllegalStateException(
                                    "finger " + finger + " already down at t=" + t);
                        }
                        if (active.isEmpty()) {
                            gestureDownTime = SystemClock.uptimeMillis();
                        }
                        active.add(finger);
                        down[finger] = true;
                        fillActive(properties, coordinates, active, curX, curY);
                        int count = active.size();
                        int action =
                                count == 1
                                        ? MotionEvent.ACTION_DOWN
                                        : MotionEvent.ACTION_POINTER_DOWN
                                                | ((count - 1)
                                                        << MotionEvent
                                                                .ACTION_POINTER_INDEX_SHIFT);
                        inject(
                                motionEvent(
                                        gestureDownTime,
                                        SystemClock.uptimeMillis(),
                                        action,
                                        count,
                                        properties,
                                        coordinates));
                    } else if (phase == 1) { // move
                        if (!down[finger]) {
                            throw new IllegalStateException(
                                    "move for finger " + finger + " while up at t=" + t);
                        }
                        fillActive(properties, coordinates, active, curX, curY);
                        inject(
                                motionEvent(
                                        gestureDownTime,
                                        SystemClock.uptimeMillis(),
                                        MotionEvent.ACTION_MOVE,
                                        active.size(),
                                        properties,
                                        coordinates));
                    } else { // up
                        if (!down[finger]) {
                            throw new IllegalStateException(
                                    "up for finger " + finger + " while up at t=" + t);
                        }
                        int ai = active.indexOf(finger);
                        if (ai < 0) {
                            throw new IllegalStateException(
                                    "finger " + finger + " missing from active");
                        }
                        fillActive(properties, coordinates, active, curX, curY);
                        int count = active.size();
                        int action =
                                count == 1
                                        ? MotionEvent.ACTION_UP
                                        : MotionEvent.ACTION_POINTER_UP
                                                | (ai
                                                        << MotionEvent
                                                                .ACTION_POINTER_INDEX_SHIFT);
                        inject(
                                motionEvent(
                                        gestureDownTime,
                                        SystemClock.uptimeMillis(),
                                        action,
                                        count,
                                        properties,
                                        coordinates));
                        active.remove(ai);
                        down[finger] = false;
                    }
                }
            }
        } finally {
            while (!active.isEmpty()) {
                fillActive(properties, coordinates, active, curX, curY);
                int count = active.size();
                int ai = count - 1;
                int action =
                        count == 1
                                ? MotionEvent.ACTION_UP
                                : MotionEvent.ACTION_POINTER_UP
                                        | (ai << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
                injectBestEffort(
                        motionEvent(
                                gestureDownTime,
                                SystemClock.uptimeMillis(),
                                action,
                                count,
                                properties,
                                coordinates));
                int f = active.remove(ai);
                down[f] = false;
            }
        }
    }

    /** Parse: multi n1 x y t ... n2 x y t ... */
    private static List<int[][]> parseMultiPaths(String[] args) {
        List<int[][]> paths = new ArrayList<>();
        int index = 1;
        while (index < args.length) {
            int n = Integer.parseInt(args[index]);
            if (n < 1) {
                throw new IllegalArgumentException("multi path length must be >= 1");
            }
            if (index + 1 + n * 3 > args.length) {
                throw new IllegalArgumentException("multi path truncated");
            }
            int[] xs = new int[n];
            int[] ys = new int[n];
            int[] ts = new int[n];
            for (int i = 0; i < n; i++) {
                xs[i] = Integer.parseInt(args[index + 1 + i * 3]);
                ys[i] = Integer.parseInt(args[index + 2 + i * 3]);
                ts[i] = Integer.parseInt(args[index + 3 + i * 3]);
            }
            validatePath(ts);
            paths.add(new int[][] {xs, ys, ts});
            index += 1 + n * 3;
        }
        if (paths.isEmpty()) {
            throw new IllegalArgumentException("multi requires at least one path");
        }
        return paths;
    }

    /** Single-finger path with absolute sample times from finger-down. */
    private void pathTimed(int[] xs, int[] ys, int[] ts) throws Exception {
        if (xs.length != ys.length || xs.length != ts.length || xs.length < 1) {
            throw new IllegalArgumentException("path needs matching x/y/t arrays");
        }
        validatePath(ts);
        MotionEvent.PointerProperties[] properties = pointerProperties(1);
        MotionEvent.PointerCoords[] coordinates = pointerCoordinates(1);
        long downTime = SystemClock.uptimeMillis();
        boolean down = false;
        try {
            setCoordinates(coordinates[0], xs[0], ys[0]);
            inject(
                    motionEvent(
                            downTime,
                            SystemClock.uptimeMillis(),
                            MotionEvent.ACTION_DOWN,
                            1,
                            properties,
                            coordinates));
            down = true;

            for (int i = 1; i < xs.length; i++) {
                sleepUntil(downTime, ts[i]);
                setCoordinates(coordinates[0], xs[i], ys[i]);
                inject(
                        motionEvent(
                                downTime,
                                SystemClock.uptimeMillis(),
                                MotionEvent.ACTION_MOVE,
                                1,
                                properties,
                                coordinates));
            }
            sleepUntil(downTime, ts[ts.length - 1]);

            inject(
                    motionEvent(
                            downTime,
                            SystemClock.uptimeMillis(),
                            MotionEvent.ACTION_UP,
                            1,
                            properties,
                            coordinates));
            down = false;
        } finally {
            if (down) {
                injectBestEffort(
                        motionEvent(
                                downTime,
                                SystemClock.uptimeMillis(),
                                MotionEvent.ACTION_UP,
                                1,
                                properties,
                                coordinates));
            }
        }
    }

    /**
     * N concurrent finger paths. Sample times are ms from gesture start.
     * Fingers may start/end at different times. Pointer IDs are stable; the
     * active pointer list is compacted on each up so ACTION_POINTER_* indices
     * stay valid.
     */
    private void multiTimed(List<int[][]> paths) throws Exception {
        int fingerCount = paths.size();
        int[] startTs = new int[fingerCount];
        int[] endTs = new int[fingerCount];
        int[] curX = new int[fingerCount];
        int[] curY = new int[fingerCount];
        boolean[] down = new boolean[fingerCount];
        List<Integer> timeline = new ArrayList<>();

        for (int f = 0; f < fingerCount; f++) {
            int[] xs = paths.get(f)[0];
            int[] ys = paths.get(f)[1];
            int[] ts = paths.get(f)[2];
            startTs[f] = ts[0];
            endTs[f] = ts[ts.length - 1];
            curX[f] = xs[0];
            curY[f] = ys[0];
            for (int t : ts) {
                int idx = Collections.binarySearch(timeline, t);
                if (idx < 0) {
                    timeline.add(~idx, t);
                }
            }
        }

        // Active pointer order: list of finger indices currently down.
        List<Integer> active = new ArrayList<>();
        MotionEvent.PointerProperties[] properties = pointerProperties(fingerCount);
        MotionEvent.PointerCoords[] coordinates = pointerCoordinates(fingerCount);
        // Stable ids 0..N-1 assigned to fingers in input order.
        for (int f = 0; f < fingerCount; f++) {
            properties[f].id = f;
        }

        long downTime = SystemClock.uptimeMillis();
        try {
            for (int t : timeline) {
                for (int f = 0; f < fingerCount; f++) {
                    int[] xs = paths.get(f)[0];
                    int[] ys = paths.get(f)[1];
                    int[] ts = paths.get(f)[2];
                    curX[f] = sampleAt(xs, ts, t, curX[f]);
                    curY[f] = sampleAt(ys, ts, t, curY[f]);
                }
                sleepUntil(downTime, t);

                // Downs first (new fingers becoming active at t).
                for (int f = 0; f < fingerCount; f++) {
                    if (!down[f] && t >= startTs[f] && t <= endTs[f]) {
                        active.add(f);
                        down[f] = true;
                        fillActive(properties, coordinates, active, curX, curY);
                        int count = active.size();
                        int action =
                                count == 1
                                        ? MotionEvent.ACTION_DOWN
                                        : MotionEvent.ACTION_POINTER_DOWN
                                                | ((count - 1)
                                                        << MotionEvent
                                                                .ACTION_POINTER_INDEX_SHIFT);
                        inject(
                                motionEvent(
                                        downTime,
                                        SystemClock.uptimeMillis(),
                                        action,
                                        count,
                                        properties,
                                        coordinates));
                    }
                }

                // Moves for currently active fingers (skip pure down-only frame).
                if (!active.isEmpty()) {
                    boolean anyMove = false;
                    for (int f : active) {
                        if (t > startTs[f] && t < endTs[f]) {
                            anyMove = true;
                            break;
                        }
                    }
                    if (anyMove) {
                        fillActive(properties, coordinates, active, curX, curY);
                        inject(
                                motionEvent(
                                        downTime,
                                        SystemClock.uptimeMillis(),
                                        MotionEvent.ACTION_MOVE,
                                        active.size(),
                                        properties,
                                        coordinates));
                    }
                }

                // Ups for fingers ending at t (highest active index first).
                for (int ai = active.size() - 1; ai >= 0; ai--) {
                    int f = active.get(ai);
                    if (t >= endTs[f]) {
                        fillActive(properties, coordinates, active, curX, curY);
                        int count = active.size();
                        int action =
                                count == 1
                                        ? MotionEvent.ACTION_UP
                                        : MotionEvent.ACTION_POINTER_UP
                                                | (ai
                                                        << MotionEvent
                                                                .ACTION_POINTER_INDEX_SHIFT);
                        inject(
                                motionEvent(
                                        downTime,
                                        SystemClock.uptimeMillis(),
                                        action,
                                        count,
                                        properties,
                                        coordinates));
                        active.remove(ai);
                        down[f] = false;
                    }
                }
            }
        } finally {
            while (!active.isEmpty()) {
                fillActive(properties, coordinates, active, curX, curY);
                int count = active.size();
                int ai = count - 1;
                int action =
                        count == 1
                                ? MotionEvent.ACTION_UP
                                : MotionEvent.ACTION_POINTER_UP
                                        | (ai << MotionEvent.ACTION_POINTER_INDEX_SHIFT);
                injectBestEffort(
                        motionEvent(
                                downTime,
                                SystemClock.uptimeMillis(),
                                action,
                                count,
                                properties,
                                coordinates));
                int f = active.remove(ai);
                down[f] = false;
            }
        }
    }

    private static void fillActive(
            MotionEvent.PointerProperties[] properties,
            MotionEvent.PointerCoords[] coordinates,
            List<Integer> active,
            int[] curX,
            int[] curY) {
        for (int i = 0; i < active.size(); i++) {
            int f = active.get(i);
            properties[i].id = f;
            properties[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
            setCoordinates(coordinates[i], curX[f], curY[f]);
        }
    }

    private static int sampleAt(int[] values, int[] times, int t, int fallback) {
        int value = fallback;
        for (int i = 0; i < times.length; i++) {
            if (times[i] <= t) {
                value = values[i];
            } else {
                break;
            }
        }
        return value;
    }

    private static void validatePath(int[] ts) {
        if (ts.length < 1) {
            throw new IllegalArgumentException("path needs at least one sample");
        }
        for (int i = 0; i < ts.length; i++) {
            if (ts[i] < 0) {
                throw new IllegalArgumentException("path times cannot be negative");
            }
            if (i > 0 && ts[i] < ts[i - 1]) {
                throw new IllegalArgumentException("path times must be non-decreasing");
            }
        }
    }

    private static void sleepUntil(long downTime, int tMs) {
        long target = downTime + tMs;
        SystemClock.sleep(Math.max(0, target - SystemClock.uptimeMillis()));
    }

    private void pinch(
            int x1Start,
            int y1Start,
            int x1End,
            int y1End,
            int x2Start,
            int y2Start,
            int x2End,
            int y2End,
            int durationMs)
            throws Exception {
        MotionEvent.PointerProperties[] properties = pointerProperties(2);
        MotionEvent.PointerCoords[] coordinates = pointerCoordinates(2);
        long downTime = SystemClock.uptimeMillis();
        boolean firstDown = false;
        boolean secondDown = false;

        setCoordinates(coordinates[0], x1Start, y1Start);
        setCoordinates(coordinates[1], x2Start, y2Start);
        try {
            inject(
                    motionEvent(
                            downTime,
                            SystemClock.uptimeMillis(),
                            MotionEvent.ACTION_DOWN,
                            1,
                            properties,
                            coordinates));
            firstDown = true;
            inject(
                    motionEvent(
                            downTime,
                            SystemClock.uptimeMillis(),
                            MotionEvent.ACTION_POINTER_DOWN
                                    | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                            2,
                            properties,
                            coordinates));
            secondDown = true;

            int frameCount =
                    Math.min(
                            PINCH_MAX_FRAMES,
                            Math.max(
                                    2,
                                    Math.round((float) durationMs / PINCH_FRAME_INTERVAL_MS)
                                            + 1));
            for (int frame = 1; frame < frameCount; frame++) {
                long targetTime =
                        downTime
                                + Math.round(
                                        (double) durationMs * frame / (frameCount - 1));
                SystemClock.sleep(Math.max(0, targetTime - SystemClock.uptimeMillis()));
                float fraction = (float) frame / (frameCount - 1);
                setCoordinates(
                        coordinates[0],
                        interpolate(x1Start, x1End, fraction),
                        interpolate(y1Start, y1End, fraction));
                setCoordinates(
                        coordinates[1],
                        interpolate(x2Start, x2End, fraction),
                        interpolate(y2Start, y2End, fraction));
                inject(
                        motionEvent(
                                downTime,
                                SystemClock.uptimeMillis(),
                                MotionEvent.ACTION_MOVE,
                                2,
                                properties,
                                coordinates));
            }

            inject(
                    motionEvent(
                            downTime,
                            SystemClock.uptimeMillis(),
                            MotionEvent.ACTION_POINTER_UP
                                    | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                            2,
                            properties,
                            coordinates));
            secondDown = false;
            inject(
                    motionEvent(
                            downTime,
                            SystemClock.uptimeMillis(),
                            MotionEvent.ACTION_UP,
                            1,
                            properties,
                            coordinates));
            firstDown = false;
        } finally {
            if (secondDown) {
                injectBestEffort(
                        motionEvent(
                                downTime,
                                SystemClock.uptimeMillis(),
                                MotionEvent.ACTION_POINTER_UP
                                        | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                                2,
                                properties,
                                coordinates));
            }
            if (firstDown) {
                injectBestEffort(
                        motionEvent(
                                downTime,
                                SystemClock.uptimeMillis(),
                                MotionEvent.ACTION_UP,
                                1,
                                properties,
                                coordinates));
            }
        }
    }

    private void inject(MotionEvent event) throws Exception {
        try {
            Object result =
                    injectInputEvent.invoke(
                            inputManager,
                            event,
                            INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH);
            if (result instanceof Boolean && !((Boolean) result)) {
                throw new IllegalStateException("InputManager rejected MotionEvent");
            }
        } catch (InvocationTargetException error) {
            Throwable cause = error.getCause();
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            throw error;
        } finally {
            event.recycle();
        }
    }

    private void injectBestEffort(MotionEvent event) {
        try {
            inject(event);
        } catch (Exception ignored) {
            // Original injection error remains authoritative.
        }
    }

    private static MotionEvent motionEvent(
            long downTime,
            long eventTime,
            int action,
            int pointerCount,
            MotionEvent.PointerProperties[] properties,
            MotionEvent.PointerCoords[] coordinates) {
        return MotionEvent.obtain(
                downTime,
                eventTime,
                action,
                pointerCount,
                properties,
                coordinates,
                0,
                0,
                1.0f,
                1.0f,
                0,
                0,
                InputDevice.SOURCE_TOUCHSCREEN,
                0);
    }

    private static MotionEvent.PointerProperties[] pointerProperties(int count) {
        MotionEvent.PointerProperties[] properties =
                new MotionEvent.PointerProperties[count];
        for (int index = 0; index < count; index++) {
            properties[index] = new MotionEvent.PointerProperties();
            properties[index].id = index;
            properties[index].toolType = MotionEvent.TOOL_TYPE_FINGER;
        }
        return properties;
    }

    private static MotionEvent.PointerCoords[] pointerCoordinates(int count) {
        MotionEvent.PointerCoords[] coordinates = new MotionEvent.PointerCoords[count];
        for (int index = 0; index < count; index++) {
            coordinates[index] = new MotionEvent.PointerCoords();
        }
        return coordinates;
    }

    private static void setCoordinates(
            MotionEvent.PointerCoords coordinates, int x, int y) {
        coordinates.clear();
        coordinates.x = x;
        coordinates.y = y;
        coordinates.pressure = 1.0f;
        coordinates.size = 1.0f;
        coordinates.touchMajor = 8.0f;
        coordinates.touchMinor = 7.0f;
    }

    private static int interpolate(int start, int end, float fraction) {
        return Math.round(start + (end - start) * fraction);
    }
}
