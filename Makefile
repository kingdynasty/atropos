tests = tests
module = atropos
#pytestops = "--full-trace"
#pytestops = "-v -s"
repo = jdidion/$(module)
desc = Release $(version)

BUILD = python setup.py build_ext -i && python setup.py install $(installargs)
TEST = py.test $(pytestops) $(tests)

all:
	$(BUILD)
	$(TEST)

install:
	$(BUILD)

test:
	$(TEST)

docs:
	make -C docs api
	make -C docs html

readme:
	pandoc --from=markdown --to=rst --output=README.rst README.md
	pandoc --from=markdown --to=rst --output=CHANGES.rst CHANGES.md

lint:
	pylint $(module)

clean:
	rm -Rf __pycache__
	rm -Rf **/__pycache__/*
	rm -Rf **/*.c
	rm -Rf **/*.so
	rm -Rf **/*.pyc
	rm -Rf dist
	rm -Rf build
	rm -Rf .adapters
	rm -Rf atropos.egg-info

docker:
	# build
	docker build -f Dockerfile -t $(repo):$(version) .
	# add alternate tags
	docker tag $(repo):$(version) $(repo):latest
	# push to Docker Hub
	docker login -u jdidion && \
	docker push $(repo)

release:
	$(clean)
	# tag
	git tag $(version)
	# build
	$(BUILD)
	$(TEST)
	python setup.py sdist bdist_wheel
	# release
	twine register dist/$(module)-$(version).tar.gz
	twine upload dist/$(module)-$(version).tar.gz
	git push origin --tags
	$(github_release)
	$(docker)

github_release:
	curl -v -i -X POST \
		-H "Content-Type:application/json" \
		-H "Authorization: token $(token)" \
		https://api.github.com/repos/$(repo)/releases \
		-d '{"tag_name":"$(version)","target_commitish": "master","name": "$(version)","body": "$(desc)","draft": false,"prerelease": false}'

# build a package with the files needed to run the workflows
workflow:
	mkdir -p dist
	tar -C paper -czf dist/atropos-paper-workflow.tgz \
		workflow/simulated.nf \
		workflow/rnaseq.nf \
		workflow/wgbs.nf \
		workflow/nextflow.config \
		workflow/run-workflows.sh \
		workflow/bin \
		containers/data/simulated/art_profiles.txt \
		containers/tools/tool-names.txt
