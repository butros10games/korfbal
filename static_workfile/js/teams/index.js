window.addEventListener("resize", setContainerHeight);

function setContainerHeight() {
  const container = document.querySelector(".teams-container");
  container.style.height = window.innerHeight - 203 + "px";
}

// Initial setting
setContainerHeight();