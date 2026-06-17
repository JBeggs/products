import unittest

from shared.shein_extract import (
    parse_shein_price_from_html,
    parse_shein_variants_from_html,
    shein_goods_id_from_url,
)

SAMPLE_SIZE_HTML = """
<div class="product-intro__size-choose" role="radiogroup" aria-label="Size">
  <div class="product-intro__size-radio" data-attr_value_name="240ml(120mlA+120mlB)" aria-label="240ml(120mlA+120mlB)">
    <p class="product-intro__size-radio-inner">240ml(120mlA+120mlB)</p>
  </div>
  <div class="product-intro__size-radio product-intro__size-radio_active" data-attr_value_name="500ml(250mlA+250mlB)" aria-checked="true">
    <p class="product-intro__size-radio-inner">500ml(250mlA+250mlB)</p>
  </div>
  <div class="product-intro__size-radio" data-attr_value_name="1000ml(500ml A+500ml B)">
    <p class="product-intro__size-radio-inner">1000ml(500ml A+500ml B)</p>
  </div>
</div>
"""


SAMPLE_PRICE_HTML = """
<div id="priceContainer" class="productPriceContainer">
  <div id="productPriceId" class="productPrice">
    <div id="productMainPriceId" class="productPrice__main" aria-label="R337" role="text">
      <span class="fs-18" aria-hidden="true">R<span class="fs-28">337</span></span>
    </div>
    <div class="productEstimatedTagNewRetail">
      <p class="productEstimatedTagNewRetail__retail" aria-label="Original Price R454">R454</p>
    </div>
  </div>
</div>
"""


SAMPLE_UV_RESIN_VARIANTS_HTML = """
<div class="product-intro__size-choose" role="radiogroup" aria-label="Size">
  <div class="product-intro__size-radio" data-attr_value_name="500g" aria-label="500g">
    <p class="product-intro__size-radio-inner">500g</p>
  </div>
  <div class="product-intro__size-radio product-intro__size-radio_active" data-attr_value_name="500g + 500g" aria-checked="true">
    <p class="product-intro__size-radio-inner">500g + 500g</p>
  </div>
</div>
"""


class SheinExtractTests(unittest.TestCase):
    def test_goods_id_from_p_url(self):
        url = "https://za.shein.com/Some-Product-p-464302913.html"
        self.assertEqual(shein_goods_id_from_url(url), "464302913")

    def test_parse_size_variants_from_html(self):
        variants = parse_shein_variants_from_html(SAMPLE_SIZE_HTML)
        self.assertEqual(
            variants,
            [
                "240ml(120mlA+120mlB)",
                "500ml(250mlA+250mlB)",
                "1000ml(500ml A+500ml B)",
            ],
        )

    def test_parse_price_from_html(self):
        self.assertEqual(parse_shein_price_from_html(SAMPLE_PRICE_HTML), 337.0)

    def test_parse_uv_resin_variants_from_html(self):
        variants = parse_shein_variants_from_html(SAMPLE_UV_RESIN_VARIANTS_HTML)
        self.assertEqual(variants, ["500g", "500g + 500g"])


if __name__ == "__main__":
    unittest.main()
