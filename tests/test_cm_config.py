import logging
import os
import sys
import unittest.mock as mock

import pytest
from jupyter_core.utils import ensure_async
from nbformat import read
from nbformat.v4.nbbase import new_code_cell, new_markdown_cell, new_notebook
from tornado.web import HTTPError

import jupytext
from jupytext.compare import compare_cells

from .utils import notebook_model

SAMPLE_NOTEBOOK = new_notebook(
    cells=[new_markdown_cell("A Markdown cell"), new_code_cell("# A code cell\n1 + 1")]
)


async def test_local_config_overrides_cm_config(cm, tmpdir):
    cm.root_dir = str(tmpdir)
    cm.formats = "ipynb,py"

    nested = tmpdir.mkdir("nested")
    with open(str(nested.join("jupytext.yml")), "w") as fp:
        fp.write("formats: ''\n")

    await cm.save(notebook_model(SAMPLE_NOTEBOOK), "notebook.ipynb")
    assert os.path.isfile(str(tmpdir.join("notebook.ipynb")))
    assert os.path.isfile(str(tmpdir.join("notebook.py")))

    await cm.save(notebook_model(SAMPLE_NOTEBOOK), "nested/notebook.ipynb")
    assert os.path.isfile(str(nested.join("notebook.ipynb")))
    assert not os.path.isfile(str(nested.join("notebook.py")))


async def test_config_file_is_called_just_once(cm, tmpdir, n=2):
    cm.root_dir = str(tmpdir)
    tmpdir.join("jupytext.toml").write("")
    nb_files = [str(tmpdir.join(f"notebook{i}.ipynb")) for i in range(n)]

    for nb_file in nb_files:
        jupytext.write(SAMPLE_NOTEBOOK, nb_file)

    mock_config = mock.MagicMock(return_value=None)

    with mock.patch(
        "jupytext.contentsmanager.load_jupytext_configuration_file", mock_config
    ):
        for i in range(n):
            await cm.get(f"notebook{i}.ipynb", content=False)

    # Listing the contents should not call the config more than once
    assert mock_config.call_count == 1


async def test_pairing_through_config_leaves_ipynb_unmodified(cm, tmpdir):
    cm.root_dir = str(tmpdir)

    cfg_file = tmpdir.join("jupytext.yml")
    nb_file = tmpdir.join("notebook.ipynb")
    py_file = tmpdir.join("notebook.py")

    cfg_file.write("formats: 'ipynb,py'\n")

    await cm.save(notebook_model(SAMPLE_NOTEBOOK), "notebook.ipynb")
    assert nb_file.isfile()
    assert py_file.isfile()

    nb = read(nb_file, as_version=4)
    assert "jupytext" not in nb.metadata


@pytest.mark.parametrize(
    "cfg_file,cfg_text",
    [
        # Should be false, not False
        ("jupytext.toml", "hide_notebook_metadata = False"),
        ("jupytext.toml", 'hide_notebook_metadata = "False"'),
        ("jupytext.toml", "not_a_jupytext_option = true"),
        ("pyproject.toml", "[tool.jupytext]\nnot_a_jupytext_option = true"),
        ("jupytext.json", '{"notebook_metadata_filter":"-all",}'),
    ],
)
@pytest.mark.filterwarnings(
    r"ignore:Passing (unrecognized|unrecoginized) arguments "
    r"to super\(JupytextConfiguration\).__init__"
)
async def test_incorrect_config_message(cm, tmpdir, cfg_file, cfg_text):
    cm.root_dir = str(tmpdir)

    tmpdir.join(cfg_file).write(cfg_text)
    tmpdir.join("empty.ipynb").write("{}")

    expected_message = "The Jupytext configuration file .*{} is incorrect".format(
        cfg_file
    )

    with pytest.raises(HTTPError, match=expected_message):
        await cm.get("empty.ipynb", type="notebook", content=False)

    with pytest.raises(HTTPError, match=expected_message):
        await cm.save(notebook_model(SAMPLE_NOTEBOOK), "notebook.ipynb")


async def test_global_config_file(cm, tmpdir):
    cm_dir = tmpdir.join("cm_dir").mkdir()
    cm.root_dir = str(cm_dir)

    tmpdir.join("jupytext.toml").write('formats = "ipynb,Rmd"')

    def fake_global_config_directory():
        return [str(tmpdir)]

    with mock.patch(
        "jupytext.config.global_jupytext_configuration_directories",
        fake_global_config_directory,
    ):
        nb = new_notebook(cells=[new_code_cell("1+1")])
        model = notebook_model(nb)
        await cm.save(model, "notebook.ipynb")
        dir_content = (await cm.get("/", content=True))["content"]
        print(dir_content)
        assert {(await ensure_async(model))["path"] for model in dir_content} == {
            "notebook.ipynb",
            "notebook.Rmd",
        }


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="AttributeError: 'LocalPath' object has no attribute 'mksymlinkto'",
)
async def test_paired_files_and_symbolic_links(cm, tmpdir):
    """We test that we don't get issues when pairing files into folders
    that are symbolic links"""

    actual = tmpdir.mkdir("actual_files")
    actual_notebooks = actual.mkdir("notebooks")
    actual_scripts = actual.mkdir("scripts")

    # Open the contents manager in another dir
    jupyter_dir = tmpdir.mkdir("jupyter_dir")
    cm.root_dir = str(jupyter_dir)

    # Create sym links to the notebook/script folders
    jupyter_dir.join("link_to_notebooks").mksymlinkto(actual_notebooks)
    jupyter_dir.join("link_to_scripts").mksymlinkto(actual_scripts)

    # Pair the notebooks in the linked folders
    jupyter_dir.join("jupytext.toml").write(
        'formats = "link_to_notebooks///ipynb,link_to_scripts///py:percent"'
    )

    # Save a notebook
    await cm.save(notebook_model(SAMPLE_NOTEBOOK), "link_to_notebooks/notebook.ipynb")

    # This creates two files in the destinations folders
    assert actual_notebooks.join("notebook.ipynb").isfile()
    assert actual_scripts.join("notebook.py").isfile()

    # Re-open the notebook (here, the text version)
    await cm.get("link_to_scripts/notebook.py")

    # Update the text version
    jupyter_dir.join("link_to_scripts").join("notebook.py").write_text(
        "# %%\n3 + 3\n", encoding="utf-8"
    )

    # Reload and make sure that we get the updated notebook
    model = await cm.get("link_to_notebooks/notebook.ipynb")
    nb = model["content"]
    compare_cells(nb.cells, [new_code_cell("3 + 3")], compare_ids=False)


async def test_metadata_filter_from_config_has_precedence_over_notebook_metadata(
    cm, tmpdir, cwd_tmpdir, python_notebook
):
    python_notebook.metadata["jupytext"] = {"notebook_metadata_filter": "-all"}
    tmpdir.join("jupytext.toml").write('notebook_metadata_filter = "all"')

    cm.root_dir = str(tmpdir)

    await cm.save(notebook_model(python_notebook), "test.py")

    py = tmpdir.join("test.py").read()
    assert "notebook_metadata_filter: all" in py


async def test_test_no_text_representation_metadata_in_ipynb_900(
    cm,
    tmpdir,
    python_notebook,
):
    tmpdir.join("jupytext.toml").write('formats = "ipynb,py:percent"\n')

    # create a test notebook and save it in Jupyter
    nb = python_notebook
    cm.root_dir = str(tmpdir)
    await cm.save(dict(type="notebook", content=nb), "test.ipynb")

    # Assert that "text_representation" is in the Jupytext metadata #900
    assert "text_representation" in tmpdir.join("test.py").read()
    # But not in the ipynb notebook
    assert "text_representation" not in tmpdir.join("test.ipynb").read()

    # modify the ipynb file in Jupyter
    # Reload the notebook
    nb = (await cm.get("test.ipynb"))["content"]
    nb.cells.append(new_markdown_cell("A new cell"))
    await cm.save(dict(type="notebook", content=nb), "test.ipynb")

    # The text representation metadata is in the py file
    assert "text_representation" in tmpdir.join("test.py").read()
    # But not in the ipynb notebook
    assert "text_representation" not in tmpdir.join("test.ipynb").read()


async def test_cm_config_no_log(cm, tmp_path, caplog):
    cm.root_dir = str(tmp_path)

    config = 'cm_config_log_level="none"'
    (tmp_path / "jupytext.toml").write_text(config)
    (tmp_path / "nb1.py").write_text("# %%")
    (tmp_path / "subfolder").mkdir()
    (tmp_path / "subfolder" / "jupytext.toml").write_text(config)
    (tmp_path / "subfolder" / "nb2.py").write_text("# %%")

    caplog.set_level(logging.DEBUG)

    await cm.get("nb1.py", type="notebook", content=False)
    await cm.get("nb1.py", type="notebook", content=True)
    await cm.get("subfolder/nb2.py", type="notebook", content=False)
    await cm.get("subfolder/nb2.py", type="notebook", content=True)

    assert "Jupytext configuration file" not in caplog.text


async def test_cm_config_log_only_if_changed(cm, tmp_path, caplog):
    cm.root_dir = str(tmp_path)

    config = ""
    (tmp_path / "jupytext.toml").write_text(config)
    (tmp_path / "nb1.py").write_text("# %%")
    (tmp_path / "subfolder").mkdir()
    (tmp_path / "subfolder" / "jupytext.toml").write_text(config)
    (tmp_path / "subfolder" / "nb2.py").write_text("# %%")

    caplog.set_level(logging.INFO)

    await cm.get("nb1.py", type="notebook", content=False)
    assert "Jupytext configuration file" in caplog.text
    caplog.clear()

    # Same notebook, same config => no log
    await cm.get("nb1.py", type="notebook", content=True)
    assert "Jupytext configuration file" not in caplog.text

    # Same notebook, config changed => log
    (tmp_path / "jupytext.toml").write_text('formats="ipynb,py:percent"')
    await cm.get("nb1.py", type="notebook", content=True)
    assert "Jupytext configuration file" in caplog.text
    caplog.clear()

    # Different folder, different config
    await cm.get("subfolder/nb2.py", type="notebook", content=False)
    assert "Jupytext configuration file" in caplog.text
    caplog.clear()

    # Same config as previously => no log
    await cm.get("subfolder/nb2.py", type="notebook", content=False)
    assert "Jupytext configuration file" not in caplog.text
