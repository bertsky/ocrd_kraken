import regex
from os.path import join
import numpy as np
from ocrd import Processor
from ocrd_utils import (
    getLogger,
    make_file_id,
    assert_file_grp_cardinality,
    coordinates_of_segment,
    coordinates_for_segment,
    bbox_from_polygon,
    points_from_polygon,
    points_from_bbox,
    polygon_from_points,
    bbox_from_points,
    transform_coordinates,
    MIMETYPE_PAGE,
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import TextEquivType, WordType, GlyphType, CoordsType, to_xml

from ocrd_kraken.config import OCRD_TOOL

class KrakenRecognize(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools']['ocrd-kraken-recognize']
        kwargs['version'] = OCRD_TOOL['version']
        super().__init__(*args, **kwargs)
        if hasattr(self, 'output_file_grp'):
            # processing context
            self.setup()

    def setup(self):
        """
        Load models
        """
        log = getLogger('processor.KrakenRecognize')
        from kraken.rpred import rpred
        from kraken.lib.models import load_any
        model_fname = self.resolve_resource(self.parameter['model'])
        log.info("loading model '%s'", model_fname)
        self.model = load_any(model_fname, device=self.parameter['device'])
        def predict(page_image, bounds):
            return rpred(self.model, page_image, bounds,
                         self.parameter['pad'],
                         self.parameter['bidi_reordering'])
        self.predict = predict

    def process(self):
        """
        Recognize with kraken
        """
        log = getLogger('processor.KrakenRecognize')
        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)

        for n, input_file in enumerate(self.input_files):
            page_id = input_file.pageId or input_file.ID
            log.info("INPUT FILE %i / %s of %s", n, page_id, len(self.input_files))
            pcgts = page_from_file(self.workspace.download_file(input_file))
            self.add_metadata(pcgts)
            page = pcgts.get_Page()
            page_image, page_coords, _ = self.workspace.image_from_page(
                page, page_id,
                feature_selector="binarized" if self.model.one_channel_mode == '1' else '')

            all_lines = page.get_AllTextLines()
            # assumes that missing baselines are rare, if any
            if any(line.Baseline for line in all_lines):
                log.info("Converting PAGE to kraken 'bounds' format (baselines)")
                bounds = {'lines': [], 'script_detection': False, 'text_direction': 'horizontal-lr', 'type': 'baselines'}
            else:
                log.info("Converting PAGE to kraken 'bounds' format (boxes only)")
                bounds = {'boxes': [], 'script_detection': False, 'text_direction': 'horizontal-lr'}
            for line in all_lines:
                # FIXME: see whether model prefers baselines or bbox crops (seg_type)
                # FIXME: even if we do not have baselines, emulating baseline+boundary might be useful to prevent automatic center normalization
                poly = coordinates_of_segment(line, None, page_coords)
                if bounds.get('type', '') == 'baselines':
                    base = baseline_of_segment(line, page_coords)
                    bounds['lines'].append({'baseline': list(map(tuple, base)),
                                            'boundary': list(map(tuple, poly)),
                                            'tags': {'type': ''}})
                else:
                    xmin, ymin, xmax, ymax = bbox_from_polygon(poly)
                    bbox = (max(xmin, 0), max(ymin, 0), min(page_image.width, xmax), min(page_image.height, ymax))
                    bounds['boxes'].append(bbox)

            for idx_line, ocr_record in enumerate(self.predict(page_image, bounds)):
                line = all_lines[idx_line]
                id_line = line.id
                if not ocr_record.prediction and not ocr_record.cuts:
                    log.warning('No results for line "%s"', line.id)
                    continue
                text_line = ocr_record.prediction
                if len(ocr_record.confidences) > 0:
                    conf_line = sum(ocr_record.confidences) / len(ocr_record.confidences)
                else:
                    conf_line = None
                line.add_TextEquiv(TextEquivType(Unicode=text_line, conf=conf_line))
                idx_word = 0
                line_offset = 0
                for text_word in regex.splititer(r'(\s+)', text_line):
                    next_offset = line_offset + len(text_word)
                    cuts_word = list(map(tuple, ocr_record.cuts[line_offset:next_offset]))
                    # fixme: kraken#98 says the Pytorch CTC output is too impoverished to yield good glyph stops
                    # as a workaround, here we just steal from the next glyph start, respectively:
                    if len(ocr_record.cuts) > next_offset + 1:
                        cuts_word.extend(list(map(tuple, ocr_record.cuts[next_offset:next_offset+1])))
                    else:
                        cuts_word.append((ocr_record.line[-1],))
                    confidences_word = ocr_record.confidences[line_offset:next_offset]
                    line_offset = next_offset
                    if len(text_word.strip()) == 0:
                        continue
                    id_word = '%s_word_%s' % (id_line, idx_word + 1)
                    idx_word += 1
                    poly_word = [point for cut in cuts_word for point in cut]
                    bbox_word = bbox_from_polygon(coordinates_for_segment(poly_word, None, page_coords))
                    # avoid zero-size coords on ties
                    bbox_word = np.array(bbox_word, dtype=int)
                    if np.prod(bbox_word[2:4] - bbox_word[0:2]) == 0:
                        bbox_word[2:4] += 1
                    if len(confidences_word) > 0:
                        conf_word = sum(confidences_word) / len(confidences_word)
                    else:
                        conf_word = None
                    word = WordType(id=id_word,
                                    Coords=CoordsType(points=points_from_bbox(*bbox_word)))
                    word.add_TextEquiv(TextEquivType(Unicode=text_word, conf=conf_word))
                    for idx_glyph, text_glyph in enumerate(text_word):
                        id_glyph = '%s_glyph_%s' % (id_word, idx_glyph + 1)
                        poly_glyph = cuts_word[idx_glyph] + cuts_word[idx_glyph + 1]
                        bbox_glyph = bbox_from_polygon(coordinates_for_segment(poly_glyph, None, page_coords))
                        # avoid zero-size coords on ties
                        bbox_glyph = np.array(bbox_glyph, dtype=int)
                        if np.prod(bbox_glyph[2:4] - bbox_glyph[0:2]) == 0:
                            bbox_glyph[2:4] += 1
                        conf_glyph = confidences_word[idx_glyph]
                        glyph = GlyphType(id=id_glyph,
                                          Coords=CoordsType(points=points_from_bbox(*bbox_glyph)))
                        glyph.add_TextEquiv(TextEquivType(Unicode=text_glyph, conf=conf_glyph))
                        word.add_Glyph(glyph)
                    line.add_Word(word)
                log.info('Recognized line "%s"', line.id)

            log.info("Finished recognition, serializing")
            file_id = make_file_id(input_file, self.output_file_grp)
            pcgts.set_pcGtsId(file_id)
            self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                mimetype=MIMETYPE_PAGE,
                local_filename=join(self.output_file_grp, f'{file_id}.xml'),
                content=to_xml(pcgts))

# zzz should go into core ocrd_utils
def baseline_of_segment(segment, coords):
    line = segment.get_Baseline()
    if line is None:
        poly = coordinates_of_segment(segment, None, coords)
        xmin, ymin, xmax, ymax = bbox_from_polygon(poly)
        ymid = ymin + 0.2 * (ymax - ymin)
        return [[xmin, ymid], [xmax, ymid]]
    line = np.array(polygon_from_points(line.points))
    line = transform_coordinates(line, coords['transform'])
    return np.round(line).astype(np.int32)
