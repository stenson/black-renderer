from contextlib import contextmanager
from io import BytesIO
import math
from fontTools.misc.transform import Transform, Identity
from fontTools.misc.arrayTools import unionRect
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.otTables import PaintFormat, VariableValue
from fontTools.ttLib.tables.otConverters import VarF2Dot14, VarFixed
from fontTools.varLib.varStore import VarStoreInstancer
import uharfbuzz as hb


PAINT_NAMES = {v.value: k for k, v in PaintFormat.__members__.items()}
PAINT_VAR_MAPPING = {
    # Map PaintVarXxx Format to its corresponding non-var Format
    v.value: PaintFormat[k.replace("PaintVar", "Paint")]
    for k, v in PaintFormat.__members__.items()
    if k.startswith("PaintVar")
}


class BlackRendererFont:
    def __init__(self, path):
        with open(path, "rb") as f:
            fontData = f.read()
        file = BytesIO(fontData)
        self.ttFont = TTFont(file, lazy=True)

        self.textColor = (0, 0, 0, 1)
        self.colrV0Glyphs = {}
        self.colrV1Glyphs = {}
        self.instancer = None

        if "COLR" in self.ttFont:
            colrTable = self.ttFont["COLR"]
            if colrTable.version == 0:
                self.colrV0Glyphs = colrTable.ColorLayers
            else:  # >= 1
                # Hm, a little sad we need to use an internal static method
                self.colrV0Glyphs = colrTable._decompileColorLayersV0(colrTable.table)
                colrTable = colrTable.table
                self.colrV1Glyphs = {
                    glyph.BaseGlyph: glyph
                    for glyph in colrTable.BaseGlyphV1List.BaseGlyphV1Record
                }
                self.colrLayersV1 = colrTable.LayerV1List
                if colrTable.VarStore is not None:
                    self.instancer = VarStoreInstancer(
                        colrTable.VarStore, self.ttFont["fvar"].axes
                    )
                else:
                    self.instancer = None

        if "CPAL" in self.ttFont:
            self.palettes = _unpackPalettes(self.ttFont["CPAL"].palettes)
        else:
            self.palettes = None
        self.paletteIndex = 0

        if "fvar" in self.ttFont:
            self.axisTags = [a.axisTag for a in self.ttFont["fvar"].axes]
        else:
            self.axisTags = []

        self.hbFont = hb.Font(hb.Face(fontData))

    @property
    def unitsPerEm(self):
        return self.hbFont.face.upem

    def setLocation(self, location):
        if location is None:
            location = {}
        self.hbFont.set_variations(location)
        if self.instancer is not None:
            normalizedAxisValues = self.hbFont.get_var_coords_normalized()
            normalizedLocation = axisValuesToLocation(
                normalizedAxisValues, self.axisTags
            )
            self.instancer.setLocation(normalizedLocation)

    @property
    def glyphNames(self):
        return self.ttFont.getGlyphOrder()

    @property
    def colrV0GlyphNames(self):
        return self.colrV0Glyphs.keys()

    @property
    def colrV1GlyphNames(self):
        return self.colrV1Glyphs.keys()

    def getGlyphBounds(self, glyphName):
        if glyphName in self.colrV1Glyphs or glyphName not in self.colrV0Glyphs:
            bounds = self._getGlyphBounds(glyphName)
        else:
            # For COLRv0, we take the union of all layer bounds
            bounds = None
            for layer in self.colrV0Glyphs[glyphName]:
                layerBounds = self._getGlyphBounds(layer.name)
                if bounds is None:
                    bounds = layerBounds
                else:
                    bounds = unionRect(layerBounds, bounds)
        return bounds

    def drawGlyph(self, glyphName, canvas):
        glyph = self.colrV1Glyphs.get(glyphName)
        if glyph is not None:
            self.currentTransform = Identity
            self.currentPath = None
            self._drawGlyphCOLRv1(glyph, canvas)
            return
        glyph = self.colrV0Glyphs.get(glyphName)
        if glyph is not None:
            self._drawGlyphCOLRv0(glyph, canvas)
            return
        else:
            self._drawGlyphNoColor(glyphName, canvas)

    def _drawGlyphNoColor(self, glyphName, canvas):
        path = canvas.newPath()
        self._drawGlyphOutline(glyphName, path)
        canvas.drawPathSolid(path, self.textColor)

    def _drawGlyphCOLRv0(self, layers, canvas):
        for layer in layers:
            path = canvas.newPath()
            self._drawGlyphOutline(layer.name, path)
            canvas.drawPathSolid(path, self._getColor(layer.colorID, 1))

    def _drawGlyphCOLRv1(self, glyph, canvas):
        self._drawPaint(glyph.Paint, canvas)

    # COLRv1 Paint dispatch

    def _drawPaint(self, paint, canvas):
        nonVarFormat = PAINT_VAR_MAPPING.get(paint.Format)
        if nonVarFormat is None:
            # "regular" Paint
            paintName = PAINT_NAMES[paint.Format]
        else:
            # PaintVar -- we map to its non-var counterpart and use a wrapper
            # that takes care of instantiating values
            paintName = PAINT_NAMES[nonVarFormat]
            paint = PaintVarWrapper(paint, self.instancer)
        drawHandler = getattr(self, "_draw" + paintName)
        drawHandler(paint, canvas)

    def _drawPaintColrLayers(self, paint, canvas):
        n = paint.NumLayers
        s = paint.FirstLayerIndex
        for i in range(s, s + n):
            with self._ensureClipAndPushPath(canvas, None):
                self._drawPaint(self.colrLayersV1.Paint[i], canvas)

    def _drawPaintSolid(self, paint, canvas):
        color = self._getColor(paint.Color.PaletteIndex, paint.Color.Alpha)
        canvas.drawPathSolid(self.currentPath, color)

    def _drawPaintLinearGradient(self, paint, canvas):
        minStop, maxStop, colorLine = self._readColorLine(paint.ColorLine)
        pt1, pt2 = _reduceThreeAnchorsToTwo(paint)
        pt1, pt2 = (
            _interpolatePoints(pt1, pt2, minStop),
            _interpolatePoints(pt1, pt2, maxStop),
        )
        canvas.drawPathLinearGradient(
            self.currentPath,
            colorLine,
            pt1,
            pt2,
            paint.ColorLine.Extend,
            self.currentTransform,
        )

    def _drawPaintRadialGradient(self, paint, canvas):
        minStop, maxStop, colorLine = self._readColorLine(paint.ColorLine)
        startCenter = (paint.x0, paint.y0)
        endCenter = (paint.x1, paint.y1)
        startCenter, endCenter = (
            _interpolatePoints(startCenter, endCenter, minStop),
            _interpolatePoints(startCenter, endCenter, maxStop),
        )
        startRadius = _interpolate(paint.r0, paint.r1, minStop)
        endRadius = _interpolate(paint.r0, paint.r1, maxStop)
        canvas.drawPathRadialGradient(
            self.currentPath,
            colorLine,
            startCenter,
            startRadius,
            endCenter,
            endRadius,
            paint.ColorLine.Extend,
            self.currentTransform,
        )

    def _drawPaintSweepGradient(self, paint, canvas):
        minStop, maxStop, colorLine = self._readColorLine(paint.ColorLine)
        center = paint.centerX, paint.centerY
        startAngle = _interpolate(paint.startAngle, paint.endAngle, minStop)
        endAngle = _interpolate(paint.startAngle, paint.endAngle, maxStop)
        canvas.drawPathSweepGradient(
            self.currentPath,
            colorLine,
            center,
            startAngle,
            endAngle,
            paint.ColorLine.Extend,
            self.currentTransform,
        )

    def _drawPaintGlyph(self, paint, canvas):
        path = canvas.newPath()
        # paint.Glyph must not be a COLR glyph
        self._drawGlyphOutline(paint.Glyph, path)
        with self._ensureClipAndPushPath(canvas, path):
            self._drawPaint(paint.Paint, canvas)

    def _drawPaintColrGlyph(self, paint, canvas):
        with self._ensureClipAndPushPath(canvas, None):
            self._drawGlyphCOLRv1(paint.Glyph, canvas)

    def _drawPaintTransform(self, paint, canvas):
        t = paint.Transform
        transform = (t.xx, t.yx, t.xy, t.yy, t.dx, t.dy)
        self._applyTransform(transform, paint.Paint, canvas)

    def _drawPaintTranslate(self, paint, canvas):
        transform = (1, 0, 0, 1, paint.dx, paint.dy)
        self._applyTransform(transform, paint.Paint, canvas)

    def _drawPaintRotate(self, paint, canvas):
        transform = Transform()
        transform = transform.translate(paint.centerX, paint.centerY)
        transform = transform.rotate(math.radians(paint.angle))
        transform = transform.translate(-paint.centerX, -paint.centerY)
        self._applyTransform(transform, paint.Paint, canvas)

    def _drawPaintSkew(self, paint, canvas):
        transform = Transform()
        transform = transform.translate(paint.centerX, paint.centerY)
        transform = transform.skew(
            math.radians(paint.xSkewAngle), math.radians(paint.ySkewAngle)
        )
        transform = transform.translate(-paint.centerX, -paint.centerY)
        self._applyTransform(transform, paint.Paint, canvas)

    def _drawPaintScale(self, paint, canvas):
        transform = Transform()
        transform = transform.translate(paint.centerX, paint.centerY)
        transform = transform.scale(paint.xScale, paint.yScale)
        transform = transform.translate(-paint.centerX, -paint.centerY)
        self._applyTransform(transform, paint.Paint, canvas)

    def _drawPaintComposite(self, paint, canvas):
        with self._ensureClipAndPushPath(canvas, None):
            print("_drawPaintComposite")
        # print("Composite with CompositeMode=", paint.CompositeMode)
        # print("Composite source:")
        # ppPaint(paint.SourcePaint, tab+1)
        # print("Composite backdrop:")
        # ppPaint(paint.BackdropPaint, tab+1)

    def _drawPaintLocation(self, paint, canvas):
        numAxes = len(self.axisTags)
        location = {
            self.axisTags[coord.AxisIndex]: coord.AxisValue
            for coord in paint.Coordinate
            if coord.AxisIndex < numAxes
        }
        with self._pushNormalizedLocation(location):
            self._drawPaint(paint.Paint, canvas)

    # Utils

    @contextmanager
    def _pushNormalizedLocation(self, location):
        orgAxisValues = self.hbFont.get_var_coords_normalized()
        tmpAxisValues = list(orgAxisValues)
        for axisIndex, axisTag in enumerate(self.axisTags):
            axisValue = location.get(axisTag)
            if axisValue is not None:
                tmpAxisValues[axisIndex] = axisValue
        tmpLocation = axisValuesToLocation(tmpAxisValues, self.axisTags)

        self.hbFont.set_var_coords_normalized(tmpAxisValues)
        if self.instancer is not None:
            orgLocation = self.instancer.location
            # FIXME: calling setLocation loses the internal instancer._scalars cache;
            # perhaps a pushing a *new* instancer and reverting to the old one is
            # faster, but this is currently not possible due to PaintVarXxx referencing
            # self.instancer, too.
            self.instancer.setLocation(tmpLocation)
            yield
            self.instancer.setLocation(orgLocation)
        else:
            yield
        self.hbFont.set_var_coords_normalized(orgAxisValues)

    @contextmanager
    def _ensureClipAndPushPath(self, canvas, path):
        clipPath = self.currentPath
        transform = self.currentTransform
        with canvas.savedState(), self._pushPath(path), self._pushTransform(Identity):
            canvas.transform(transform)
            if clipPath is not None:
                canvas.clipPath(clipPath)
            yield

    @contextmanager
    def _pushPath(self, path):
        currentPath = self.currentPath
        self.currentPath = path
        yield
        self.currentPath = currentPath

    @contextmanager
    def _pushTransform(self, transform):
        currentTransform = self.currentTransform
        self.currentTransform = transform
        yield
        self.currentTransform = currentTransform

    def _applyTransform(self, transform, paint, canvas):
        self.currentTransform = self.currentTransform.transform(transform)
        self._drawPaint(paint, canvas)

    def _drawGlyphOutline(self, glyphName, path):
        gid = self.ttFont.getGlyphID(glyphName)
        self.hbFont.draw_glyph_with_pen(gid, path)

    def _getGlyphBounds(self, glyphName):
        gid = self.ttFont.getGlyphID(glyphName)
        x, y, w, h = self.hbFont.get_glyph_extents(gid)
        # convert from HB's x/y_bearing + extents to xMin, yMin, xMax, yMax
        y += h
        h = -h
        w += x
        h += y
        return x, y, w, h

    def _getColor(self, colorIndex, alpha):
        if colorIndex == 0xFFFF:
            # TODO: find text foreground color
            r, g, b, a = self.textColor
        else:
            r, g, b, a = self.palettes[self.paletteIndex][colorIndex]
        a *= alpha
        return r, g, b, a

    def _readColorLine(self, colorLineTable):
        return _normalizeColorLine(
            [
                (cs.StopOffset, self._getColor(cs.Color.PaletteIndex, cs.Color.Alpha))
                for cs in colorLineTable.ColorStop
            ]
        )


def _reduceThreeAnchorsToTwo(p):
    # FIXME: make sure the 3 points are not in degenerate position [see COLRv1 spec].
    x02 = p.x2 - p.x0
    y02 = p.y2 - p.y0
    x01 = p.x1 - p.x0
    y01 = p.y1 - p.y0
    squaredNorm02 = x02 * x02 + y02 * y02
    k = (x01 * x02 + y01 * y02) / squaredNorm02
    x = p.x1 - k * x02
    y = p.y1 - k * y02
    return ((p.x0, p.y0), (x, y))


def _normalizeColorLine(colorLine):
    stops = [stopOffset for stopOffset, color in colorLine]
    minStop = min(stops)
    maxStop = max(stops)
    if minStop != maxStop:
        stopExtent = maxStop - minStop
        colorLine = [
            ((stopOffset - minStop) / stopExtent, color)
            for stopOffset, color in colorLine
        ]
    else:
        # Degenerate case. minStop and maxStop are used to reposition
        # the gradients parameters (points, radii, angles), through
        # interpolation. By setting minStop and maxStop to 0 and 1,
        # we at least won't mess up the parameters.
        # FIXME: should the gradient simply not be drawn in this case?
        minStop, maxStop = 0, 1
    return minStop, maxStop, colorLine


def _interpolate(v1, v2, f):
    return v1 + f * (v2 - v1)


def _interpolatePoints(pt1, pt2, f):
    x1, y1 = pt1
    x2, y2 = pt2
    return (x1 + f * (x2 - x1), y1 + f * (y2 - y1))


def _unpackPalettes(palettes):
    return [
        [(c.red / 255, c.green / 255, c.blue / 255, c.alpha / 255) for c in p]
        for p in palettes
    ]


def axisValuesToLocation(normalizedAxisValues, axisTags):
    return {
        axisTag: axisValue for axisTag, axisValue in zip(axisTags, normalizedAxisValues)
    }


_conversionFactors = {
    VarF2Dot14: 1 / (1 << 14),
    VarFixed: 1 / (1 << 16),
}


class PaintVarWrapper:
    def __init__(self, wrappedPaint, instancer):
        assert not isinstance(wrappedPaint, PaintVarWrapper)
        self._wrappedPaint = wrappedPaint
        self._instancer = instancer

    def __repr__(self):
        return f"PaintVarWrapper({self.obj!r})"

    def __getattr__(self, attrName):
        value = getattr(self._wrappedPaint, attrName)
        if isinstance(value, VariableValue):
            if value.varIdx != 0xFFFFFFFF:
                factor = _conversionFactors.get(
                    type(self._wrappedPaint.getConverterByName(attrName)), 1
                )
                value = value.value + self._instancer[value.varIdx] * factor
            else:
                value = value.value
        elif type(value).__name__.startswith("Var"):
            value = PaintVarWrapper(value, self._instancer)
        elif isinstance(value, list):
            value = [PaintVarWrapper(item, self._instancer) for item in value]
        return value
