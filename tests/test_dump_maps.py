from PIL import Image

from scripts.dump_maps import (
    affine_compose,
    affine_from_transform,
    affine_point,
    choose_ppu,
    composite_clipped,
)


def test_affine_composes_parent_rotation_scale_and_child_translation():
    parent = affine_from_transform(
        {"x": 10, "y": 20}, {"z": 2 ** -0.5, "w": 2 ** -0.5}, {"x": 2, "y": 3}
    )
    child = affine_from_transform({"x": 1, "y": 2}, {}, {"x": -1, "y": 1})

    transform = affine_compose(parent, child)

    assert affine_point(transform, 0, 0) == (4.0, 22.0)
    x, y = affine_point(transform, 1, 0)
    assert round(x, 10) == 4.0
    assert round(y, 10) == 20.0


def test_composite_clipped_keeps_overlapping_pixels():
    canvas = Image.new("RGBA", (3, 3))
    sprite = Image.new("RGBA", (3, 3), "red")

    assert composite_clipped(canvas, sprite, -2, 1)
    assert canvas.getpixel((0, 1)) == (255, 0, 0, 255)
    assert canvas.getpixel((1, 1)) == (0, 0, 0, 0)


def test_choose_ppu_reduces_to_dimension_limit():
    assert choose_ppu(200, 10, requested=32) == 16
