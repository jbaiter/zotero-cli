from setuptools import setup, find_packages

setup(
    name="zotero-cli",
    version="0.3.0",
    description="Command-line interface for the Zotero API",
    author="Johannes Baiter",
    author_email="johannes.baiter@gmail.com",
    url="https://github.com/jbaiter/zotero-cli",
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        "setuptools-git",
        "Click >= 6.6",
        "pypandoc >= 1.1.3",
        "Pyzotero >= 1.1.15",
        "pathlib >= 1.0.1",
        "rauth >= 0.7.2"],
    entry_points="""
        [console_scripts]
        zotcli=zotero_cli.cli:cli
    """)
