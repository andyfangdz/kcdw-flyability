"""Compact SVG paths without changing hourly data or gap boundaries."""
from .event_renderer import Chart


def straight_points(points):
    result = []
    for point in points:
        while len(result) >= 2:
            a, b = result[-2:]
            ab = (b[0]-a[0], b[1]-a[1])
            bc = (point[0]-b[0], point[1]-b[1])
            if abs(ab[0]*bc[1]-ab[1]*bc[0]) > 1e-7 or ab[0]*bc[0]+ab[1]*bc[1] < 0:
                break
            result.pop()
        result.append(point)
    return result


class MoistureChart(Chart):
    def _segments(self, values):
        return [straight_points(segment) for segment in super()._segments(values)]

    def band(self, low, high, fill, opacity=.13):
        # Shared implementation already preserves gaps/singletons. Simplify each
        # generated polygon before publication, never the tooltip values array.
        start = len(self.parts)
        super().band(low, high, fill, opacity)
        import re
        def compress(match):
            tokens = match.group(1).split()
            points = [tuple(map(float, t.split(','))) for t in tokens]
            return 'points="' + ' '.join(f'{x:g},{y:g}' for x,y in straight_points(points)) + '"'
        self.parts[start:] = [re.sub(r'points="([^"]+)"', compress, part) for part in self.parts[start:]]
