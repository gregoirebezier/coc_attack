package coc.farm2;

import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.MotionEvent;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

public final class PinchInjector {
    private static final int INJECT_INPUT_EVENT_MODE_WAIT_FOR_FINISH = 2;
    private static final int MAX_FRAMES = 21;
    private static final int FRAME_INTERVAL_MS = 25;

    private final Object inputManager;
    private final Method injectInputEvent;

    private PinchInjector() throws ReflectiveOperationException {
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
        if (args.length != 9) {
            throw new IllegalArgumentException(
                    "expected x1Start y1Start x1End y1End "
                            + "x2Start y2Start x2End y2End durationMs");
        }
        int[] values = new int[args.length];
        for (int index = 0; index < args.length; index++) {
            values[index] = Integer.parseInt(args[index]);
        }
        if (values[8] <= 0) {
            throw new IllegalArgumentException("durationMs must be positive");
        }
        if (values[0] == values[4] && values[1] == values[5]) {
            throw new IllegalArgumentException(
                    "pinch pointers must start at different points");
        }

        new PinchInjector()
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
        MotionEvent.PointerProperties[] properties = pointerProperties();
        MotionEvent.PointerCoords[] coordinates = pointerCoordinates();
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
                            MAX_FRAMES,
                            Math.max(
                                    2,
                                    Math.round((float) durationMs / FRAME_INTERVAL_MS)
                                            + 1));
            for (int frame = 1; frame < frameCount; frame++) {
                long targetTime =
                        downTime
                                + Math.round(
                                        (double) durationMs
                                                * frame
                                                / (frameCount - 1));
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
            // The original injection error remains authoritative.
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

    private static MotionEvent.PointerProperties[] pointerProperties() {
        MotionEvent.PointerProperties[] properties =
                new MotionEvent.PointerProperties[2];
        for (int index = 0; index < properties.length; index++) {
            properties[index] = new MotionEvent.PointerProperties();
            properties[index].id = index;
            properties[index].toolType = MotionEvent.TOOL_TYPE_FINGER;
        }
        return properties;
    }

    private static MotionEvent.PointerCoords[] pointerCoordinates() {
        MotionEvent.PointerCoords[] coordinates = new MotionEvent.PointerCoords[2];
        for (int index = 0; index < coordinates.length; index++) {
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
