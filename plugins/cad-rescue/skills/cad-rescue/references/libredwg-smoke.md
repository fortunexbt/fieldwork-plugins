# LibreDWG adapter smoke evidence

Checked 2026-09-30 against the public `sample_2000.dwg` test fixture in the
[LibreDWG repository](https://github.com/LibreDWG/libredwg/blob/master/test/test-data/sample_2000.dwg).
The repository describes the project under GPL version 3 or later in its
[README](https://github.com/LibreDWG/libredwg#license). The fixture is not included
in CAD Rescue or in its examples.

- Upstream file: `test/test-data/sample_2000.dwg`
- Attribution: LibreDWG project test data and contributors
- Size: 22,027 bytes
- SHA-256: `4b8d9b1396a6d0decaa328dfc7cf17a09f2afa716ff8f40e9ca62ad1f1504ffa`
- Runtime: `@mlightcad/libredwg-web` 0.7.9, Node.js 24.18.0, and ezdxf 1.4.4
- Conversion: generated a DXF that ezdxf reopened with 6 model-space entities and
  no audit errors; the resulting DXF was 53,888 bytes.
- Inspection: reported 2 layers and declared millimeter units from the converted
  DXF. Computed model-space extents were x=-50.3187372548..219.6812627452 and
  y=-85.3042408761..194.6957591239.
- Preview: generated 5 vector paths; the text entity was omitted and reported.
  `rsvg-convert` rendered the SVG at 1600×1200. The preview is linework regenerated
  from the DXF export, not the original DWG rendering.

These results establish a working optional conversion route for this R2000 sample.
They do not establish full DWG feature fidelity, performance on large drawings, or
support for every DWG version. LibreDWG's own project documentation describes
limitations for advanced R2010+ objects.
