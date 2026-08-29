from typing import NamedTuple

import pulumi


class ImageVersions:
    _config = pulumi.Config('image')

    def __getitem__(self, name: str) -> str:
        try:
            return self._config.require(name)
        except pulumi.ConfigMissingError as e:
            raise KeyError(f'Image {name} does not exist in config') from e


class ChartVersion(NamedTuple):
    repo: str
    version: str


class ChartVersions:
    _config = pulumi.Config('chart')

    def __getitem__(self, name: str) -> ChartVersion:
        try:
            repo, version = self._config.require(name).rsplit(':', 1)
        except pulumi.ConfigMissingError as e:
            raise KeyError(f'Chart {name} does not exist in config') from e
        return ChartVersion(repo, version)


class Versions:
    image = ImageVersions()
    chart = ChartVersions()


versions = Versions()
