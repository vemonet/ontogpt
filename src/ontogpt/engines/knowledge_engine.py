"""
Main Knowledge Extractor class.
"""
import importlib
import logging
import re
from urllib.parse import quote
from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Optional, TextIO, Tuple, Union

import openai
import pydantic
import yaml
from linkml_runtime import SchemaView
from linkml_runtime.linkml_model import ClassDefinition, ElementName, SlotDefinition
from oaklib import BasicOntologyInterface, get_implementation_from_shorthand
from oaklib.datamodels.text_annotator import TextAnnotationConfiguration
from oaklib.implementations import OntoPortalImplementationBase
from oaklib.interfaces import MappingProviderInterface, TextAnnotatorInterface
from oaklib.utilities.apikey_manager import get_apikey_value
from oaklib.utilities.subsets.value_set_expander import ValueSetExpander

from ontogpt.clients import OpenAIClient
from ontogpt.templates.core import ExtractionResult, NamedEntity

this_path = Path(__file__).parent
logger = logging.getLogger(__name__)


OBJECT = Union[str, pydantic.BaseModel, dict]
EXAMPLE = OBJECT
FIELD = str
TEMPLATE_NAME = str

# annotation metamodel
ANNOTATION_KEY_PROMPT = "prompt"
ANNOTATION_KEY_PROMPT_SKIP = "prompt.skip"
ANNOTATION_KEY_ANNOTATORS = "annotators"
ANNOTATION_KEY_RECURSE = "ner.recurse"
ANNOTATION_KEY_EXAMPLES = "prompt.examples"

# TODO: introspect
DATAMODELS = [
    "treatment.DiseaseTreatmentSummary",
    "gocam.GoCamAnnotations",
    "bioloigical_process.BiologicalProcess",
    "environmental_sample.Study",
    "mendelian_disease.MendelianDisease",
    "reaction.Reaction",
    "recipe.Recipe",
]


def chunk_text(text: str, window_size=3) -> Iterator[str]:
    """
    Chunk text into windows of sentences.
    """
    sentences = re.split(r"[.?!]\s+", text)
    for right_index in range(1, len(sentences)):
        left_index = max(0, right_index - window_size)
        yield ". ".join(sentences[left_index:right_index])


@dataclass
class KnowledgeEngine(ABC):
    """
    Abstract base class for all knowledge engines
    """

    template: TEMPLATE_NAME = None
    """LinkML Template to use for this engine.
    Must be of the form <module_name>.<ClassName>"""

    template_class: ClassDefinition = None
    """LinkML Class for the template.
    This is derived from the template and does not need to be set manually."""

    template_pyclass = None
    """Python class for the template.
    This is derived from the template and does not need to be set manually."""

    template_module: ModuleType = None
    """Python module for the template.
    This is derived from the template and does not need to be set manually."""

    schemaview: SchemaView = None
    """LinkML SchemaView over the template.
    This is derived from the template and does not need to be set manually."""

    api_key: str = None
    """OpenAI API key."""

    engine: str = None
    """OpenAI Engine. This should be overridden in subclasses"""

    # annotator: TextAnnotatorInterface = None
    # """Default annotator. TODO: deprecate?"""

    annotators: Dict[str, List[TextAnnotatorInterface]] = None
    """Annotators for each class.
    An annotator will ground/map labels to CURIEs.
    These override the annotators annotated in the template
    """

    mappers: List[BasicOntologyInterface] = None
    """List of concept mappers, to assist in grounding to desired ID prefix"""

    labelers: List[BasicOntologyInterface] = None
    """Labelers that map CURIEs to labels"""

    client: OpenAIClient = None
    """All calls to LLMs are delegated through this client"""

    dictionary: Dict[str, str] = field(default_factory=dict)
    """Local dictionary of strings/labels to IDs"""

    value_set_expansions: Dict[str, List[str]] = field(default_factory=dict)

    min_grounding_text_overlap = 0.66
    """Min proportion of overlap in characters between text and grounding. TODO: use tokenization"""

    named_entities: List[NamedEntity] = field(default_factory=list)
    """Cache of all named entities"""

    auto_prefix: str = None
    """If set then non-normalized named entities will be mapped to this prefix"""

    last_text: str = None
    """Cache of last text."""

    last_prompt: str = None
    """Cache of last prompt used."""

    grounding: bool = True
    """Ground entities."""

    def __post_init__(self):
        if self.template:
            self.template_class = self._get_template_class(self.template)
        self.client = OpenAIClient()
        self.api_key = self._get_openai_api_key()
        openai.api_key = self.api_key
        if self.mappers is None:
            self.mappers = [get_implementation_from_shorthand("translator:")]

    def extract_from_text(
        self, text: str, cls: ClassDefinition = None, object: OBJECT = None
    ) -> ExtractionResult:
        raise NotImplementedError

    def extract_from_file(self, file: Union[str, Path, TextIO]) -> pydantic.BaseModel:
        """
        Extract annotations from the given text.

        :param file:
        :return:
        """
        if isinstance(file, str):
            file = Path(file)
        if isinstance(file, Path):
            with file.open() as f:
                text = f.read()
        else:
            text = file.read()
        self.last_text = text
        r = self.extract_from_text(text)
        r.input_id = str(file)
        return r

    def load_dictionary(self, path: Union[str, Path, list]):
        if not isinstance(path, list):
            logger.info(f"Loading dictionary from {path}")
            with open(str(path)) as f:
                return self.load_dictionary(yaml.safe_load(f))
        if self.dictionary is None:
            self.dictionary = {}
        entries = [(entry["synonym"].lower(), entry["id"]) for entry in path]
        entries = sorted(entries, key=lambda x: len(x[0]), reverse=True)
        for syn, id in entries:
            if syn in self.dictionary and self.dictionary[syn] != id:
                logger.warning(f"Duplicate synonym: {syn} => {id}, {self.dictionary[syn]}")
            self.dictionary[syn] = id
        logger.info(f"Loaded {len(self.dictionary)}")

    def synthesize(self, cls: ClassDefinition = None, object: OBJECT = None) -> ExtractionResult:
        pass

    def generalize(
        self, object: Union[pydantic.BaseModel, dict], examples: List[EXAMPLE]
    ) -> ExtractionResult:
        raise NotImplementedError

    def map_terms(self, terms: List[str], ontology: str) -> Dict[str, List[str]]:
        raise NotImplementedError

    def _get_template_class(self, template: TEMPLATE_NAME) -> ClassDefinition:
        """
        Get the LinkML class for a template.

        :param template: template name of the form module.ClassName
        :return: LinkML class definition
        """
        logger.info(f"Loading schema for {template}")
        module_name, class_name = template.split(".", 1)
        templates_path = this_path.parent / "templates"
        path_to_template = str(templates_path / f"{module_name}.yaml")
        mod = importlib.import_module(f"ontogpt.templates.{module_name}")
        self.template_module = mod
        self.template_pyclass = mod.__dict__[class_name]
        sv = SchemaView(path_to_template)
        self.schemaview = sv
        logger.info(f"Getting class for template {template}")
        cls = None
        for c in sv.all_classes().values():
            if c.name == class_name:
                cls = c
                break
        if not cls:
            raise ValueError(f"Template {template} not found")
        return cls

    def _get_openai_api_key(self):
        """Get the OpenAI API key from the environment."""
        # return os.environ.get("OPENAI_API_KEY")
        return get_apikey_value("openai")

    def get_annotators(self, cls: ClassDefinition = None) -> List[BasicOntologyInterface]:
        """
        Get the annotators/labelers for a class.

        The annotators are returned in order of precedence

        Annotators are used to *ground* labels as CURIEs.
        Annotators may also do double-duty as labelers (i.e. map CURIEs to labels)

        These are specified by linkml annotations within the template/schema;
        if the engine has a set of annotators specified these take precedence.

        :param cls: schema class
        :return: list of annotations
        """
        if self.annotators and cls.name in self.annotators:
            annotators = self.annotators[cls.name]
        else:
            if ANNOTATION_KEY_ANNOTATORS not in cls.annotations:
                logger.error(f"No annotators for {cls.name}")
                return []
            annotators = cls.annotations[ANNOTATION_KEY_ANNOTATORS].value.split(", ")
        logger.info(f" Annotators: {annotators}")
        objs = []
        for annotator in annotators:
            if isinstance(annotator, str):
                logger.info(f"Loading annotator {annotator}")
                objs.append(get_implementation_from_shorthand(annotator))
            elif isinstance(annotator, BasicOntologyInterface):
                objs.append(annotator)
            else:
                raise ValueError(f"Unknown annotator type {annotator}")
        return objs

    def promptable_slots(self, cls: Optional[ClassDefinition] = None) -> List[SlotDefinition]:
        """
        List of all slots that are not skipped for purposes of prompting.

        Examples of slots that are skipped are:

        - identifier fields
        - the source text used in extraction
        - other metadata that is outside what we might want to predict

        :param cls:
        :return:
        """
        if cls is None:
            cls = self.template_class
        sv = self.schemaview
        return [s for s in sv.class_induced_slots(cls.name) if not self.slot_is_skipped(s)]

    def slot_is_skipped(self, slot: SlotDefinition) -> bool:
        sv = self.schemaview
        if ANNOTATION_KEY_PROMPT_SKIP in slot.annotations:
            return True

    def normalize_named_entity(self, text: str, range: ElementName) -> str:
        """
        Grounds and normalizes to preferred ID prefixes.

        :param text:
        :param cls:
        :return:
        """
        sv = self.schemaview
        cls = sv.get_class(range)
        if cls is None:
            return text
        if ANNOTATION_KEY_EXAMPLES in cls.annotations:
            examples = cls.annotations[ANNOTATION_KEY_EXAMPLES].value.split(", ")
            examples = [x.lower() for x in examples]
            logger.debug(f"Will exclude if in list of examples: {examples}")
            if text.lower() in examples:
                logger.warning(f"Likely a hallucination as it is the example set: {text}")
                return f"LIKELY HALLUCINATION: {text}"
        for obj_id in self.groundings(text, cls):
            logger.info(f"Grounding {text} to {obj_id}; next step is to normalize")
            for normalized_id in self.normalize_identifier(obj_id, cls):
                self.named_entities.append(NamedEntity(id=obj_id, label=text))
                logger.info(f"Normalized {text} with {obj_id} to {normalized_id}")
                return normalized_id
        logger.info(f"Could not ground and normalize {text} to {cls.name}")
        if self.auto_prefix:
            obj_id = f"{self.auto_prefix}:{quote(text)}"
        else:
            obj_id = text
        if ANNOTATION_KEY_RECURSE in cls.annotations:
            logger.info(f"Using recursive strategy to parse: {text} to {cls.name}")
            obj = self.extract_from_text(text, cls).extracted_object
            if obj:
                if self.named_entities is None:
                    self.named_entities = []
                try:
                    obj.id = obj_id
                except ValueError as e:
                    logger.error(f"No id for {obj} {e}")
                self.named_entities.append(obj)
        return obj_id

    def is_valid_identifier(self, input_id: str, cls: ClassDefinition) -> bool:
        sv = self.schemaview
        if cls.id_prefixes:
            if ":" not in input_id:
                return False
            prefix, rest = input_id.split(":", 1)
            if prefix not in cls.id_prefixes:
                logger.debug(f"ID {input_id} not in prefixes {cls.id_prefixes}")
                return False
        id_slot = sv.get_identifier_slot(cls.name)
        if id_slot and id_slot.pattern:
            id_regex = re.compile(id_slot.pattern)
            m = re.match(id_regex, input_id)
            if not m:
                logger.debug(f"ID {input_id} does not match pattern {id_slot.pattern}")
                return False
        if id_slot and id_slot.values_from:
            vse = ValueSetExpander()
            for e in id_slot.values_from:
                if e not in self.value_set_expansions:
                    range_enum = sv.get_enum(e)
                    pvs = vse.expand_value_set(range_enum, sv.schema)
                    valid_ids = [pv.text for pv in pvs]
                    self.value_set_expansions[e] = valid_ids
                    logger.info(f"Expanded {e} to {len(valid_ids)} IDs")
                if input_id not in self.value_set_expansions[e]:
                    logger.info(f"ID {input_id} not in value set {e}")
                    return False
        return True

    def normalize_identifier(self, input_id: str, cls: ClassDefinition) -> Iterator[str]:
        if self.is_valid_identifier(input_id, cls):
            yield input_id
        for obj_id in self.map_identifier(input_id, cls):
            if obj_id == input_id:
                continue
            if self.is_valid_identifier(obj_id, cls):
                yield obj_id

    def map_identifier(self, input_id: str, cls: ClassDefinition) -> Iterator[str]:
        """
        Normalize an identifier to a preferred prefix.

        :param input_id:
        :param cls:
        :return:
        """
        if input_id.startswith("http://purl.bioontology.org/ontology"):
            # TODO: this should be fixed upstream in OAK
            logging.info(f"Normalizing BioPortal id {input_id}")
            input_id = input_id.replace("http://purl.bioontology.org/ontology/", "").replace(
                "/", ":"
            )
        if input_id.startswith("http://id.nlm.nih.gov/mesh/"):
            # TODO: this should be fixed upstream in OAK
            logging.info(f"Normalizing MESH id {input_id}")
            input_id = input_id.replace("http://id.nlm.nih.gov/mesh/", "").replace("/", ":")
        if input_id.startswith("drugbank:"):
            input_id = input_id.replace("drugbank:", "DRUGBANK:")
        yield input_id
        if not cls.id_prefixes:
            return
        if not self.mappers:
            return
        for mapper in self.mappers:
            if isinstance(mapper, MappingProviderInterface):
                for mapping in mapper.sssom_mappings([input_id]):
                    yield str(mapping.object_id)
            else:
                raise ValueError(f"Unknown mapper type {mapper}")

    def groundings(self, text: str, cls: ClassDefinition) -> Iterator[str]:
        """
        Ground the given text to element identifiers.

        :param text: text to ground, e.g. gene symbol
        :param cls: schema class the ground object should instantiate
        :return:
        """
        if self.grounding:
            logger.info(f"GROUNDING {text} using {cls.name}")
            text_lower = text.lower()
            if self.dictionary and text_lower in self.dictionary:
                obj_id = self.dictionary[text_lower]
                logger.info(f"Found {text} in dictionary: {obj_id}")
                yield obj_id
            if self.dictionary:
                for syn, obj_id in self.dictionary.items():
                    if syn in text_lower:
                        if len(syn) / len(text_lower) > self.min_grounding_text_overlap:
                            logger.info(f"Found {syn} < {text} in dictionary: {obj_id}")
                            yield obj_id
            if self.annotators and cls.name in self.annotators:
                annotators = self.annotators[cls.name]
            else:
                if ANNOTATION_KEY_ANNOTATORS not in cls.annotations:
                    annotators = []
                else:
                    annotators = cls.annotations[ANNOTATION_KEY_ANNOTATORS].value.split(", ")
            logger.info(f" Annotators: {annotators}")
            # prioritize whole matches by running these first
            for matches_whole_text in [True, False]:
                config = TextAnnotationConfiguration(matches_whole_text=matches_whole_text)
                for annotator in annotators:
                    if isinstance(annotator, str):
                        logger.info(f"Loading annotator {annotator}")
                        annotator = get_implementation_from_shorthand(annotator)
                    if not matches_whole_text and not isinstance(
                        annotator, OntoPortalImplementationBase
                    ):
                        # TODO: allow more fine-grained control
                        logger.info(
                            f"Skipping {type(annotator)} as it does not support partial matches"
                        )
                        continue
                    try:
                        results = annotator.annotate_text(text, config)
                        for result in results:
                            yield result.object_id
                    except Exception as e:
                        logger.error(f"Error with {annotator} for {text}: {e}")

    # def ground_text_to_id(self, text: str, cls: ClassDefinition = None) -> str:
    #    raise NotImplementedError

    def merge_resultsets(
        self, resultset: List[ExtractionResult], unique_fields: List[str] = None
    ) -> ExtractionResult:
        """
        Merges all resultsets into a single resultset.

        Note the first element of the list is mutated.

        :param resultset:
        :return:
        """
        result = resultset[0].extracted_object
        for next_extraction in resultset[1:]:
            next_result = next_extraction.extracted_object
            if unique_fields:
                for k in unique_fields:
                    if k in result and k in next_result:
                        if result[k] != next_result[k]:
                            logger.error(
                                f"Cannot merge unique fields: {k} {result[k]} != {next_result[k]}"
                            )
                            continue
            for k, v in vars(next_result).items():
                curr_v = getattr(result, k, None)
                if isinstance(v, list):
                    if all(isinstance(x, str) for x in v):
                        setattr(result, k, list(set(curr_v).union(set(v))))
                    else:
                        setattr(result, k, curr_v + v)
                else:
                    if curr_v and v and curr_v != v:
                        logger.error(f"Cannot merge {curr_v} and {v}")
                    if v:
                        setattr(result, k, v)
        return resultset[0]
