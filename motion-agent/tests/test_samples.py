"""見本の props.json（Remotion Studio の既定値）が、今の timing・meta から作り直した結果と一致するか。
落ちたら `python -m app.tools.build_samples` を流す。"""
from app.core.templates import list_samples, list_templates, template_dir
from app.tools.build_samples import render_sample


def test_sample_props_are_up_to_date():
    stale = []
    for t in list_templates():
        for name in list_samples(t["template_id"], t["version"]):
            path = template_dir(t["template_id"], t["version"]) / "samples" / f"{name}.props.json"
            if not path.is_file() or path.read_text(encoding="utf-8") != render_sample(t["template_id"], t["version"], name):
                stale.append(path.name)
    assert stale == [], f"python -m app.tools.build_samples を流してください: {stale}"


def test_every_template_has_samples():
    for t in list_templates():
        assert t["samples"], t["template_id"]
